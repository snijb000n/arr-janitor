#!/usr/bin/env python3
"""
arr-janitor — nachtelijke import/cleanup voor Radarr + Sonarr.

Subcommando's:
  extract   safety-net: pak achtergebleven .rar/.zip/.7z uit in completed/
  import    push manualimport-kandidaten met zekere match door
  clean     verwijder stalled/failed queue-items + blocklist + optionele re-search
  all       extract -> import -> clean (cron-entry)

Globale flags: --dry-run, --verbose
Config: ./config.env naast dit script.
"""
from __future__ import annotations

import argparse
import difflib
import fcntl
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.env"
LOG_PATH = SCRIPT_DIR / "arr_janitor.log"
LOCK_PATH = SCRIPT_DIR / ".lock"
EXTRACT_MARKER_SUFFIX = ".extracted"
ARCHIVE_EXTS = {".rar", ".zip", ".7z"}
STALLED_PATTERNS = re.compile(
    r"stalled|no files found|corrupt|truncated|incomplete|aborted|unable to import",
    re.IGNORECASE,
)
# Alleen écht-onderweg of -klaar states zijn no-touch.
SAFE_TRACKED_STATES = {"downloading", "queued", "imported", "importing"}
# trackedDownloadState waarden die direct mislukt zijn.
FAILED_TRACKED_STATES = {"downloadfailed", "failedpending", "blocked", "ignored"}
# Pending-states: kunnen spook-items zijn (download bestand verdwenen of niet matchbaar).
PENDING_TRACKED_STATES = {"importpending", "importblocked"}

log = logging.getLogger("arr-janitor")


@dataclass(frozen=True)
class ArrInstance:
    name: str
    url: str
    api_key: str
    downloads_host: Path
    downloads_container: str
    is_sonarr: bool


@dataclass
class Config:
    radarr: ArrInstance
    sonarr: ArrInstance
    min_file_age_min: int
    stalled_hours: int
    max_remove_per_run: int
    max_import_per_run: int
    research_after_blocklist: bool
    import_mode: str
    fuzzy_match_enabled: bool
    fuzzy_match_threshold: float

    def all_roots(self) -> list[Path]:
        return [self.radarr.downloads_host, self.sonarr.downloads_host]

    def instances(self) -> list[ArrInstance]:
        return [self.radarr, self.sonarr]


# ---------- config ----------

def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"FATAL: missing config file {path}")
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_config() -> Config:
    e = load_env(CONFIG_PATH)
    required = [
        "RADARR_URL", "RADARR_API_KEY",
        "SONARR_URL", "SONARR_API_KEY",
        "RADARR_DOWNLOADS_HOST", "RADARR_DOWNLOADS_CONTAINER",
        "SONARR_DOWNLOADS_HOST", "SONARR_DOWNLOADS_CONTAINER",
    ]
    missing = [k for k in required if not e.get(k)]
    if missing:
        sys.exit(f"FATAL: config.env missing keys: {missing}")

    def as_int(k: str, default: int) -> int:
        v = e.get(k)
        return int(v) if v else default

    def as_bool(k: str, default: bool) -> bool:
        v = e.get(k, "").lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        return default

    radarr = ArrInstance(
        name="radarr",
        url=e["RADARR_URL"].rstrip("/"),
        api_key=e["RADARR_API_KEY"],
        downloads_host=Path(e["RADARR_DOWNLOADS_HOST"]).resolve(),
        downloads_container=e["RADARR_DOWNLOADS_CONTAINER"].rstrip("/"),
        is_sonarr=False,
    )
    sonarr = ArrInstance(
        name="sonarr",
        url=e["SONARR_URL"].rstrip("/"),
        api_key=e["SONARR_API_KEY"],
        downloads_host=Path(e["SONARR_DOWNLOADS_HOST"]).resolve(),
        downloads_container=e["SONARR_DOWNLOADS_CONTAINER"].rstrip("/"),
        is_sonarr=True,
    )
    import_mode = e.get("IMPORT_MODE", "Copy").strip()
    if import_mode not in ("Copy", "Move", "Auto"):
        sys.exit(f"FATAL: IMPORT_MODE must be Copy|Move|Auto, got {import_mode!r}")
    try:
        fuzzy_threshold = float(e.get("FUZZY_MATCH_THRESHOLD", "0.75"))
    except ValueError:
        fuzzy_threshold = 0.75
    return Config(
        radarr=radarr,
        sonarr=sonarr,
        min_file_age_min=as_int("MIN_FILE_AGE_MIN", 15),
        stalled_hours=as_int("STALLED_HOURS", 2),
        max_remove_per_run=as_int("MAX_REMOVE_PER_RUN", 20),
        max_import_per_run=as_int("MAX_IMPORT_PER_RUN", 50),
        research_after_blocklist=as_bool("RESEARCH_AFTER_BLOCKLIST", True),
        import_mode=import_mode,
        fuzzy_match_enabled=as_bool("FUZZY_MATCH_ENABLED", True),
        fuzzy_match_threshold=fuzzy_threshold,
    )


# ---------- logging + locking ----------

def setup_logging(verbose: bool) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)


class _LockHeld(Exception):
    pass


def acquire_lock():
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise _LockHeld()
    return fd


# ---------- HTTP ----------

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "DELETE"]),
        raise_on_status=False,
    )
    a = HTTPAdapter(max_retries=retry)
    s.mount("http://", a)
    s.mount("https://", a)
    return s


def arr_get(s: requests.Session, inst: ArrInstance, path: str, **params):
    headers = {"X-Api-Key": inst.api_key}
    r = s.get(f"{inst.url}/api/v3{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def arr_post(s: requests.Session, inst: ArrInstance, path: str, body):
    headers = {"X-Api-Key": inst.api_key, "Content-Type": "application/json"}
    r = s.post(f"{inst.url}/api/v3{path}", headers=headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


def arr_delete(s: requests.Session, inst: ArrInstance, path: str, **params):
    headers = {"X-Api-Key": inst.api_key}
    r = s.delete(f"{inst.url}/api/v3{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


# ---------- path safety ----------

def in_root(path: Path, roots: Iterable[Path]) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def file_age_minutes(p: Path) -> float:
    try:
        return (time.time() - p.stat().st_mtime) / 60.0
    except OSError:
        return 0.0


# ---------- extract ----------

def find_extractor(ext: str) -> list[str] | None:
    if ext in (".zip", ".7z"):
        if shutil.which("7z"):
            return ["7z", "x", "-y", "-bso0", "-bsp0"]
        return None
    if ext == ".rar":
        if shutil.which("unrar"):
            return ["unrar", "x", "-o+", "-y"]
        if shutil.which("unar"):
            return ["unar", "-f"]
        return None
    return None


def cmd_extract(cfg: Config, dry_run: bool) -> None:
    roots = cfg.all_roots()
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            log.warning("extract: root missing %s", root)
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in ARCHIVE_EXTS:
                continue
            if not in_root(p, roots):
                log.warning("extract: skip outside-root %s", p)
                continue
            marker = p.with_name(p.name + EXTRACT_MARKER_SUFFIX)
            if marker.exists():
                continue
            if file_age_minutes(p) < cfg.min_file_age_min:
                log.debug("extract: too young, skip %s", p)
                continue
            # multi-volume rar: only extract part01.rar / .rar (not .r00, .r01, .partNN.rar > 1)
            stem = p.stem.lower()
            if re.search(r"\.part0*[2-9]\d*$|\.part0*[1-9]\d{2,}$", stem):
                continue
            if p.suffix.lower() == ".rar" and re.search(r"\.r\d{2,}$", p.name.lower()):
                continue
            candidates.append(p)

    if not candidates:
        log.info("extract: nothing to do")
        return

    log.info("extract: %d archive(s) to handle", len(candidates))
    for arc in candidates:
        ext = arc.suffix.lower()
        cmd = find_extractor(ext)
        if not cmd:
            log.warning("extract: no extractor for %s (install unrar?), skip %s", ext, arc)
            continue
        if dry_run:
            log.info("[dry-run] extract %s -> %s", arc, arc.parent)
            continue
        log.info("extract: %s", arc)
        try:
            subprocess.run(
                cmd + [str(arc)],
                cwd=str(arc.parent),
                check=True,
                capture_output=True,
                timeout=3600,
            )
            arc.with_name(arc.name + EXTRACT_MARKER_SUFFIX).touch()
            log.info("extract: ok %s", arc)
        except subprocess.CalledProcessError as ex:
            log.error("extract: failed %s rc=%s stderr=%s",
                      arc, ex.returncode, ex.stderr[-400:].decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            log.error("extract: timeout %s", arc)


# ---------- import ----------

def _confident_radarr(item: dict) -> bool:
    if item.get("rejections"):
        return False
    if not item.get("movie") and not item.get("movieId"):
        return False
    movie_id = item.get("movieId") or (item.get("movie") or {}).get("id") or 0
    return movie_id > 0


def _confident_sonarr(item: dict) -> bool:
    if item.get("rejections"):
        return False
    series_id = item.get("seriesId") or (item.get("series") or {}).get("id") or 0
    if series_id <= 0:
        return False
    eps = item.get("episodes") or []
    ep_ids = item.get("episodeIds") or [e.get("id") for e in eps if e.get("id")]
    return len([e for e in ep_ids if e]) > 0


def _build_import_payload(item: dict, is_sonarr: bool) -> dict:
    payload = {
        "path": item["path"],
        "quality": item.get("quality"),
        "languages": item.get("languages") or [],
        "downloadId": item.get("downloadId"),
    }
    if is_sonarr:
        payload["seriesId"] = item.get("seriesId") or (item.get("series") or {}).get("id")
        eps = item.get("episodes") or []
        payload["episodeIds"] = item.get("episodeIds") or [e["id"] for e in eps if e.get("id")]
        payload["episodeFileId"] = 0
    else:
        payload["movieId"] = item.get("movieId") or (item.get("movie") or {}).get("id")
    return payload


def _scan_candidates(inst: ArrInstance, s: requests.Session, **params) -> list[dict]:
    try:
        return arr_get(s, inst, "/manualimport", filterExistingFiles="true", **params)
    except requests.HTTPError as ex:
        log.error("import[%s]: manualimport %s failed: %s", inst.name, params, ex)
        return []


_NOISE_PATTERN = re.compile(
    r"\b(1080p|2160p|720p|480p|x265|x264|h\.?26[45]|hevc|avc|web-?dl|web|webrip|"
    r"bluray|brrip|amzn|nf|netflix|hulu|disney|max|mgmp|atvp|viki|"
    r"aac|ac3|ddp?|atmos|truehd|dts(-?hd)?|eac3|flac|"
    r"hdr|sdr|dv|dovi|10bit|8bit|"
    r"repack|proper|internal|complete|uhd|imax|extended|directors?|remastered|"
    r"multi|dual(-?audio)?|jap-?eng|eng-?jap|english|dub|sub|"
    r"v\d+|s\d{1,2}e?\d{0,3}|s\d{1,2}|season\s*\d+|episode\s*\d+|"
    r"part\s*\d+|cd\d+)\b",
    re.IGNORECASE,
)
_YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")
_TRAILING_GROUP = re.compile(r"-[a-z0-9]+$", re.IGNORECASE)


def _normalize_title(s: str, is_filename: bool = False) -> str:
    """Reduce string to vergelijkbare titel-tokens.

    Voor filenames: pak het stuk vóór de eerste SxxExx of (jaar) marker —
    daarna gewone normalisatie. Voor library-titles: alleen losse tokens.
    """
    if not s:
        return ""
    # strip extensie
    s = re.sub(r"\.[a-z0-9]{2,4}$", "", s, flags=re.IGNORECASE)
    # strip trailing release group (-ABC123) vóór separator-replacement
    s = re.sub(r"-[A-Za-z0-9]+$", "", s)
    if is_filename:
        # knip op SxxExx, SxE, of (YYYY) marker — pak alleen het deel ervoor
        cut = re.search(r"\bS\d{1,2}(?:E\d{1,3})?\b|\b(?:19|20)\d{2}\b", s, re.IGNORECASE)
        if cut:
            s = s[:cut.start()]
    s = re.sub(r"[._\-]+", " ", s)
    s = _YEAR_PATTERN.sub(" ", s)
    s = _NOISE_PATTERN.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)  # apostrofs, dubbelpunten, kommas weg
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _fuzzy_pick(name: str, candidates: list[tuple[int, str]],
                threshold: float) -> tuple[int | None, float]:
    """Return (best_id, score). Score is 0..1. id=None als onder threshold."""
    n = _normalize_title(name, is_filename=True)
    if not n:
        return (None, 0.0)
    best_score = 0.0
    best_id: int | None = None
    for cid, title in candidates:
        nt = _normalize_title(title, is_filename=False)
        if not nt:
            continue
        score = difflib.SequenceMatcher(None, n, nt).ratio()
        if score > best_score:
            best_score = score
            best_id = cid
    return (best_id if best_score >= threshold else None, best_score)


# Rejections die we NOOIT mogen overrulen (kwaliteit/sample/pack-issues zijn
# bewuste *arr-beslissingen, geen parser-mismatch).
_BLOCKING_REJECTION = re.compile(
    r"not an upgrade|not a custom format upgrade|sample|"
    r"single episode file contains all episodes",
    re.IGNORECASE,
)
# Rejections die wél recoverable zijn via fuzzy series-match en/of episode-resolve.
_RECOVERABLE_REJECTION = re.compile(
    r"unknown series|unknown movie|invalid season or episode|"
    r"episode \d+x\d+ was unexpected",
    re.IGNORECASE,
)


def _classify_rejections(rejections: list) -> tuple[bool, bool]:
    """Return (has_recoverable, has_blocking).

    Een item is alleen safe-to-recover als has_recoverable=True én has_blocking=False.
    """
    if not rejections:
        return (False, False)
    has_recoverable = False
    has_blocking = False
    for r in rejections:
        reason = (r.get("reason") or "").lower() if isinstance(r, dict) else str(r).lower()
        if _BLOCKING_REJECTION.search(reason):
            has_blocking = True
        if _RECOVERABLE_REJECTION.search(reason):
            has_recoverable = True
    return (has_recoverable, has_blocking)


def _is_recoverable_rejection(rejections: list) -> bool:
    """True als alle rejections recoverable zijn (geen blocking ones)."""
    has_recoverable, has_blocking = _classify_rejections(rejections)
    return has_recoverable and not has_blocking


# Getallen die NOOIT een absoluut episode-nummer zijn (codecs, resoluties, jaren).
_FALSE_EP_NUMBERS = {264, 265, 360, 480, 720, 1080, 1440, 2160, 4320}


def _extract_absolute_candidates(filename: str) -> list[int]:
    """Pak waarschijnlijke absoluut-episode-nummers uit filename.

    Heuristieken (van strikt naar losser):
      1. `Episode\s*N` → expliciet (zeer betrouwbaar)
      2. `S\d+E(N)` waar N >= 100 → absoluut in E-slot (anime mis-tag)
      3. ` - N ` of `]-N[` of `]N[` → anime release pattern
    Filtert jaartallen (1900-2099), codecs en resoluties.
    """
    candidates: list[int] = []

    def _add(n: int) -> None:
        if n in _FALSE_EP_NUMBERS:
            return
        if 1900 <= n <= 2099:
            return
        if 1 <= n <= 9999 and n not in candidates:
            candidates.append(n)

    for m in re.finditer(r"\bEpisode[\s._-]*(\d{1,4})\b", filename, re.IGNORECASE):
        _add(int(m.group(1)))
    for m in re.finditer(r"\bS\d{1,2}E(\d{3,4})\b", filename, re.IGNORECASE):
        _add(int(m.group(1)))
    for m in re.finditer(r"(?:\s|\]|^)-?\s*(\d{1,4})(?=\s|\[|\(|\.|$)", filename):
        n = int(m.group(1))
        # bredere fallback: alleen accepteren als het géén SxxExx-context heeft
        if not re.search(rf"S\d+E0*{n}\b", filename, re.IGNORECASE):
            _add(n)
    return candidates


def _resolve_episode_ids(inst: ArrInstance, s: requests.Session,
                         series_id: int, filename: str) -> list[int]:
    """Resolve episode-ids voor `filename` binnen `series_id`.

    Strategy 1: SxxExx parse + season/episode lookup (klassiek tv).
    Strategy 2: absoluut episode-nummer lookup (anime / mis-tagged files).

    Returns lege lijst bij ambiguïteit (>1 match) om verkeerde toewijzing te vermijden.
    """
    # Strategy 1: SxxExx
    m = re.search(r"S(\d{1,2})E(\d{1,3})", filename, re.IGNORECASE)
    if m:
        season, ep_num = int(m.group(1)), int(m.group(2))
        try:
            eps = arr_get(s, inst, "/episode", seriesId=series_id, seasonNumber=season)
            ids = [e["id"] for e in eps if e.get("episodeNumber") == ep_num]
            if ids:
                return ids
        except requests.HTTPError as ex:
            log.warning("resolve[%s]: episode lookup failed seriesId=%s s%s: %s",
                        inst.name, series_id, season, ex)

    # Strategy 2: absoluut episode-nummer
    candidates = _extract_absolute_candidates(filename)
    if not candidates:
        return []
    try:
        all_eps = arr_get(s, inst, "/episode", seriesId=series_id)
    except requests.HTTPError as ex:
        log.warning("resolve[%s]: full episode fetch failed seriesId=%s: %s",
                    inst.name, series_id, ex)
        return []
    abs_map: dict[int, list[int]] = {}
    for e in all_eps:
        a = e.get("absoluteEpisodeNumber")
        if a:
            abs_map.setdefault(a, []).append(e["id"])
    # Pak eerste kandidaat die exact 1 episode oplevert
    for n in candidates:
        ids = abs_map.get(n)
        if ids and len(ids) == 1:
            log.debug("resolve[%s]: absolute %s -> episodeId %s :: %s",
                      inst.name, n, ids[0], filename)
            return ids
    return []


def _try_fuzzy_match(inst: ArrInstance, s: requests.Session, cfg: Config,
                     skipped: list[dict]) -> list[dict]:
    """Return extra import-payloads voor recoverable-rejection items.

    Twee soorten recoverable:
      A. 'Unknown Series/Movie' → series/movie onbekend, fuzzy-match nodig
      B. 'Invalid season or episode' / 'Episode X was unexpected' →
         series wél bekend (seriesId/movieId in item), alleen episode mismatch
    """
    if not cfg.fuzzy_match_enabled or not skipped:
        return []
    targets = [it for it in skipped if _is_recoverable_rejection(it.get("rejections"))]
    if not targets:
        return []

    # Pool alleen ophalen als minstens 1 target fuzzy-match nodig heeft.
    needs_fuzzy = any(
        not (it.get("seriesId") or (it.get("series") or {}).get("id")
             or it.get("movieId") or (it.get("movie") or {}).get("id"))
        for it in targets
    )
    pool: list[tuple[int, str]] = []
    if needs_fuzzy:
        if inst.is_sonarr:
            try:
                for sr in arr_get(s, inst, "/series"):
                    pool.append((sr["id"], sr.get("title", "")))
                    for alt in sr.get("alternateTitles") or []:
                        title = alt.get("title")
                        if title:
                            pool.append((sr["id"], title))
            except requests.HTTPError as ex:
                log.error("fuzzy[%s]: series pool fetch failed: %s", inst.name, ex)
                return []
        else:
            try:
                for mv in arr_get(s, inst, "/movie"):
                    pool.append((mv["id"], mv.get("title", "")))
                    for alt in mv.get("alternateTitles") or []:
                        title = alt.get("title")
                        if title:
                            pool.append((mv["id"], title))
            except requests.HTTPError as ex:
                log.error("fuzzy[%s]: movie pool fetch failed: %s", inst.name, ex)
                return []

    payloads: list[dict] = []
    for it in targets:
        path = it.get("path") or ""
        fname = Path(path).stem

        if inst.is_sonarr:
            # Bepaal seriesId — direct uit item als die er is, anders fuzzy.
            sid = it.get("seriesId") or (it.get("series") or {}).get("id") or 0
            if sid:
                series_source = "direct"
                score = 1.0
            else:
                sid, score = _fuzzy_pick(fname, pool, cfg.fuzzy_match_threshold)
                series_source = "fuzzy"
                if not sid:
                    log.info("recover[sonarr]: no series match (best %.2f) :: %s",
                             score, fname)
                    continue

            ep_ids = _resolve_episode_ids(inst, s, sid, fname)
            if not ep_ids:
                log.info("recover[sonarr]: seriesId=%s (%s, score %.2f) maar episode niet resolvable :: %s",
                         sid, series_source, score, fname)
                continue
            log.info("recover[sonarr]: seriesId=%s episodeIds=%s (%s, score %.2f) :: %s",
                     sid, ep_ids, series_source, score, fname)
            payloads.append({
                "path": path,
                "seriesId": sid,
                "episodeIds": ep_ids,
                "quality": it.get("quality"),
                "languages": it.get("languages") or [{"id": 1, "name": "English"}],
                "downloadId": it.get("downloadId"),
                "episodeFileId": 0,
            })
        else:
            mid = it.get("movieId") or (it.get("movie") or {}).get("id") or 0
            if mid:
                movie_source = "direct"
                score = 1.0
            else:
                mid, score = _fuzzy_pick(fname, pool, cfg.fuzzy_match_threshold)
                movie_source = "fuzzy"
                if not mid:
                    log.info("recover[radarr]: no movie match (best %.2f) :: %s",
                             score, fname)
                    continue
            log.info("recover[radarr]: movieId=%s (%s, score %.2f) :: %s",
                     mid, movie_source, score, fname)
            payloads.append({
                "path": path,
                "movieId": mid,
                "quality": it.get("quality"),
                "languages": it.get("languages") or [],
                "downloadId": it.get("downloadId"),
            })
    return payloads


def _import_for(inst: ArrInstance, s: requests.Session, cfg: Config, dry_run: bool) -> None:
    confident_check = _confident_sonarr if inst.is_sonarr else _confident_radarr

    # Bron 1: top-level scan van completed-folder (vangt files die nog niet in queue zitten).
    log.info("import[%s]: scanning folder %s", inst.name, inst.downloads_container)
    folder_items = _scan_candidates(inst, s, folder=inst.downloads_container)

    # Bron 2: queue items die op manualimport wachten — dit is Svens hoofdcase.
    queue_items: list[dict] = []
    download_ids: set[str] = set()
    try:
        page = arr_get(s, inst, "/queue", pageSize=2000,
                       includeUnknownMovieItems="true",
                       includeUnknownSeriesItems="true")
        records = page.get("records") if isinstance(page, dict) else page or []
        for q in records:
            state = (q.get("trackedDownloadState") or "").lower()
            track = (q.get("trackedDownloadStatus") or "").lower()
            if state in ("importpending", "importblocked") or (
                track == "warning" and state in ("", "import_pending")
            ):
                did = q.get("downloadId")
                if did and did not in download_ids:
                    download_ids.add(did)
        log.info("import[%s]: %d queue item(s) wait on manualimport", inst.name, len(download_ids))
    except requests.HTTPError as ex:
        log.warning("import[%s]: queue scan for manualimport failed: %s", inst.name, ex)

    for did in download_ids:
        queue_items.extend(_scan_candidates(inst, s, downloadId=did))

    # Dedupe op path
    seen: set[str] = set()
    items: list[dict] = []
    for it in folder_items + queue_items:
        p = it.get("path")
        if not p or p in seen:
            continue
        seen.add(p)
        items.append(it)

    if not items:
        log.info("import[%s]: API returned no candidates", inst.name)
        return

    confident: list[dict] = []
    skipped: list[dict] = []
    for it in items:
        if confident_check(it):
            confident.append(it)
        else:
            skipped.append(it)
            log.warning("import[%s]: skip path=%s rejections=%s",
                        inst.name, it.get("path"),
                        [r.get("reason") for r in it.get("rejections", [])] or "no-id")

    files = [_build_import_payload(it, inst.is_sonarr) for it in confident]

    # Fuzzy-match fallback voor 'Unknown Series/Movie' rejections.
    fuzzy_payloads = _try_fuzzy_match(inst, s, cfg, skipped)
    if fuzzy_payloads:
        log.info("import[%s]: fuzzy-match recovered %d extra file(s)",
                 inst.name, len(fuzzy_payloads))
        files.extend(fuzzy_payloads)

    if not files:
        log.info("import[%s]: 0 importable of %d candidate(s) (skipped %d, fuzzy 0)",
                 inst.name, len(items), len(skipped))
        return

    if len(files) > cfg.max_import_per_run:
        log.warning("import[%s]: %d files exceeds cap %d, capping",
                    inst.name, len(files), cfg.max_import_per_run)
        files = files[:cfg.max_import_per_run]

    body = {"name": "ManualImport", "files": files, "importMode": cfg.import_mode}

    if dry_run:
        for f in files:
            log.info("[dry-run] import[%s] %s", inst.name, f["path"])
        return

    try:
        resp = arr_post(s, inst, "/command", body)
        log.info("import[%s]: queued %d file(s) cmd=%s",
                 inst.name, len(files), resp.get("id"))
    except requests.HTTPError as ex:
        log.error("import[%s]: ManualImport command failed: %s body=%s",
                  inst.name, ex, ex.response.text[:400] if ex.response else "")


def cmd_import(cfg: Config, dry_run: bool) -> None:
    s = make_session()
    for inst in cfg.instances():
        _import_for(inst, s, cfg, dry_run)


# ---------- clean ----------

def _is_stalled(item: dict, stalled_hours: int, host_root_map: dict[str, Path] | None = None) -> bool:
    """Verwijderwaardig als:
      - status=failed OF state=downloadFailed/failedPending/blocked/ignored
      - state=importPending/importBlocked én download-pad is leeg/weg of bevat 'no files'/'unable to import'
      - error/statusMessage matcht stalled-pattern
    Conservatief: actief downloadende of geslaagd-geïmporteerde items nooit aanraken.
    """
    status = (item.get("status") or "").lower()
    state = (item.get("trackedDownloadState") or "").lower()
    err = item.get("errorMessage") or ""
    status_messages = item.get("statusMessages") or []
    msg_text = " ".join(
        m.get("title", "") + " " + " ".join(m.get("messages") or [])
        for m in status_messages
    )

    if state in SAFE_TRACKED_STATES:
        return False

    pattern_hit = bool(STALLED_PATTERNS.search(err) or STALLED_PATTERNS.search(msg_text))

    bad = (
        status == "failed"
        or state in FAILED_TRACKED_STATES
        or (state in PENDING_TRACKED_STATES and pattern_hit)
        or pattern_hit
    )

    # Spook-detectie: importPending zonder bestaand bestand op disk.
    if not bad and state in PENDING_TRACKED_STATES and host_root_map:
        out = item.get("outputPath") or ""
        host_path = _container_to_host(out, host_root_map)
        if host_path and not host_path.exists():
            bad = True

    if not bad:
        return False

    added = item.get("added") or item.get("estimatedCompletionTime")
    if not added:
        return True
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(added.replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        return age_h >= stalled_hours
    except (ValueError, TypeError):
        return True


def _container_to_host(container_path: str, host_root_map: dict[str, Path]) -> Path | None:
    """Map een *arr-container pad naar host pad via geconfigureerde prefixen."""
    if not container_path:
        return None
    for cprefix, hprefix in host_root_map.items():
        if container_path == cprefix or container_path.startswith(cprefix + "/"):
            rel = container_path[len(cprefix):].lstrip("/")
            return hprefix / rel if rel else hprefix
    return None


def _trigger_research(s, inst: ArrInstance, item: dict) -> None:
    try:
        if inst.is_sonarr:
            ep_ids = [e.get("id") for e in (item.get("episode") and [item["episode"]] or [])]
            ep_ids = [e for e in ep_ids if e]
            if ep_ids:
                arr_post(s, inst, "/command", {"name": "EpisodeSearch", "episodeIds": ep_ids})
                log.info("clean[%s]: re-search EpisodeSearch ids=%s", inst.name, ep_ids)
                return
            sid = item.get("seriesId")
            if sid:
                arr_post(s, inst, "/command", {"name": "SeriesSearch", "seriesId": sid})
                log.info("clean[%s]: re-search SeriesSearch series=%s", inst.name, sid)
        else:
            mid = item.get("movieId")
            if mid:
                arr_post(s, inst, "/command", {"name": "MoviesSearch", "movieIds": [mid]})
                log.info("clean[%s]: re-search MoviesSearch movie=%s", inst.name, mid)
    except requests.HTTPError as ex:
        log.warning("clean[%s]: re-search failed: %s", inst.name, ex)


_PHANTOM_REJECTION = re.compile(
    r"was unexpected considering the.*folder name|"
    r"was not found in the grabbed release",
    re.IGNORECASE,
)


def _cleanup_recover_phantoms(inst: ArrInstance, s: requests.Session,
                              records: list[dict], dry_run: bool) -> set[int]:
    """Detecteer en clean queue-rows die spook-restanten zijn van succesvolle
    recover-imports (typisch: pack-grabs waar Sonarr's parser meer episodes
    verwachtte dan de pack feitelijk bevatte).

    Definitie phantom: row in importPending/importBlocked met rejection
    "was unexpected" / "not found in the grabbed release", waarbij voor
    dezelfde `downloadId` reeds een `downloadFolderImported` history-event
    bestaat — de file is dus al onder een ander (echt) episodeId binnen,
    de queue-row blijft hangen omdat Sonarr's parser de mismatch niet
    zelf oplost. Cleanup zonder blocklist — geen re-grab risico, geen
    file-actie (file blijft op disk).

    Returns: set van qid's die zijn verwijderd (zodat caller die uit
    stalled-detection kan filteren).
    """
    if not inst.is_sonarr:
        return set()  # Radarr heeft geen episode-mismatch packs

    candidates: list[dict] = []
    for q in records:
        state = (q.get("trackedDownloadState") or "").lower()
        if state not in PENDING_TRACKED_STATES:
            continue
        if not q.get("outputPath"):
            continue
        sms = q.get("statusMessages") or []
        text = " ".join(
            (m.get("title", "") or "") + " " + " ".join(m.get("messages") or [])
            for m in sms
        )
        if not _PHANTOM_REJECTION.search(text):
            continue
        candidates.append(q)

    if not candidates:
        return set()

    # Sonarr history voor downloadFolderImported events (eventType=3).
    # ManualImport events hebben downloadId=None maar wel data.droppedPath —
    # daaruit halen we de pack-folder waar de file vandaan kwam.
    try:
        hist = arr_get(s, inst, "/history",
                       pageSize=500, sortKey="date",
                       sortDirection="descending", eventType=3)
        hist_recs = hist.get("records", [])
    except requests.HTTPError as ex:
        log.warning("phantom[%s]: history fetch failed: %s", inst.name, ex)
        return set()

    imported_folders: set[str] = set()
    for h in hist_recs:
        data = h.get("data") or {}
        dp = data.get("droppedPath")
        if dp:
            imported_folders.add(str(Path(dp).parent))

    phantoms = [q for q in candidates if q.get("outputPath") in imported_folders]
    if not phantoms:
        log.debug("phantom[%s]: %d candidate row(s), 0 met succesvolle import",
                  inst.name, len(candidates))
        return set()

    by_folder: dict[str, list[dict]] = {}
    for p in phantoms:
        by_folder.setdefault(p["outputPath"], []).append(p)
    log.info("phantom[%s]: %d row(s) over %d pack-folder(s) waar files al binnen zijn",
             inst.name, len(phantoms), len(by_folder))

    removed: set[int] = set()
    for q in phantoms:
        qid = q.get("id")
        if dry_run:
            log.info("[dry-run] phantom[%s] would remove qid=%s outputPath=%s",
                     inst.name, qid, (q.get("outputPath") or "")[:80])
            removed.add(qid)
            continue
        try:
            arr_delete(s, inst, f"/queue/{qid}",
                       removeFromClient="false", blocklist="false",
                       skipRedownload="true")
            removed.add(qid)
        except requests.HTTPError as ex:
            if ex.response is not None and ex.response.status_code == 404:
                removed.add(qid)
            else:
                log.warning("phantom[%s]: delete qid=%s failed: %s", inst.name, qid, ex)
    log.info("phantom[%s]: removed %d/%d row(s)", inst.name, len(removed), len(phantoms))
    return removed


def _clean_for(inst: ArrInstance, s: requests.Session, cfg: Config, dry_run: bool) -> None:
    try:
        page = arr_get(s, inst, "/queue",
                       pageSize=2000, includeUnknownMovieItems="true",
                       includeUnknownSeriesItems="true")
    except requests.HTTPError as ex:
        log.error("clean[%s]: queue GET failed: %s", inst.name, ex)
        return
    records = page.get("records") if isinstance(page, dict) else page
    if not records:
        log.info("clean[%s]: queue empty", inst.name)
        return

    # Eerst spook-rijen van succesvolle recover-imports opruimen (geen blocklist).
    phantom_qids = _cleanup_recover_phantoms(inst, s, records, dry_run)
    if phantom_qids:
        records = [r for r in records if r.get("id") not in phantom_qids]

    host_map = {inst.downloads_container: inst.downloads_host}

    # Groep per downloadId (rijen zonder downloadId = eigen group per row-id).
    groups: dict[str, list[dict]] = {}
    for it in records:
        gid = it.get("downloadId") or f"_norow_{it.get('id')}"
        groups.setdefault(gid, []).append(it)

    # Stalled-check op group-niveau: als één rij in de group stalled is,
    # markeer hele group voor verwijdering.
    stalled_groups: list[tuple[str, list[dict]]] = []
    for gid, rows in groups.items():
        if any(_is_stalled(r, cfg.stalled_hours, host_map) for r in rows):
            stalled_groups.append((gid, rows))

    if not stalled_groups:
        log.info("clean[%s]: %d in queue (%d groups), 0 stalled",
                 inst.name, len(records), len(groups))
        return

    if len(stalled_groups) > cfg.max_remove_per_run:
        log.warning("clean[%s]: %d stalled groups exceeds cap %d, capping",
                    inst.name, len(stalled_groups), cfg.max_remove_per_run)
        stalled_groups = stalled_groups[:cfg.max_remove_per_run]

    total_rows = sum(len(rows) for _, rows in stalled_groups)
    log.info("clean[%s]: %d stalled group(s) covering %d row(s)",
             inst.name, len(stalled_groups), total_rows)

    for gid, rows in stalled_groups:
        title = rows[0].get("title") or gid
        if dry_run:
            log.info("[dry-run] clean[%s] remove group downloadId=%s rows=%d title=%s",
                     inst.name, gid, len(rows), title)
            continue

        removed = 0
        for r in rows:
            qid = r.get("id")
            try:
                arr_delete(s, inst, f"/queue/{qid}",
                           removeFromClient="true", blocklist="true")
                removed += 1
            except requests.HTTPError as ex:
                # Multi-row groups: zodra één DELETE met removeFromClient=true slaagt,
                # ruimt Sonarr/Radarr alle andere queue rows met dezelfde downloadId
                # in één klap op. Volgende DELETEs krijgen dan 404 — dat is success.
                if ex.response is not None and ex.response.status_code == 404:
                    removed += 1
                    log.debug("clean[%s]: qid=%s al weg (404, group-cascade)", inst.name, qid)
                else:
                    log.error("clean[%s]: delete qid=%s failed: %s", inst.name, qid, ex)

        log.info("clean[%s]: removed group downloadId=%s rows=%d/%d title=%s",
                 inst.name, gid, removed, len(rows), title)

        # Eén re-search per group, niet per row.
        if removed and cfg.research_after_blocklist:
            _trigger_research(s, inst, rows[0])


def cmd_clean(cfg: Config, dry_run: bool) -> None:
    s = make_session()
    for inst in cfg.instances():
        _clean_for(inst, s, cfg, dry_run)


# ---------- entry ----------

def main() -> int:
    p = argparse.ArgumentParser(description="arr-janitor")
    p.add_argument("command", choices=["extract", "import", "clean", "all"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    setup_logging(args.verbose)
    cfg = load_config()

    try:
        lock_fd = acquire_lock()
    except _LockHeld:
        log.info("previous run still active, exiting")
        return 0

    log.info("=== arr-janitor %s start (dry_run=%s, import_mode=%s) ===",
             args.command, args.dry_run, cfg.import_mode)
    try:
        if args.command in ("extract", "all"):
            cmd_extract(cfg, args.dry_run)
        if args.command in ("import", "all"):
            cmd_import(cfg, args.dry_run)
        if args.command in ("clean", "all"):
            cmd_clean(cfg, args.dry_run)
        log.info("=== done ===")
        return 0
    except Exception:
        log.exception("unhandled error")
        return 1
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
