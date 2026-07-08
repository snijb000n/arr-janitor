#!/usr/bin/env python3
"""
arr-janitor — nachtelijke import/cleanup voor Radarr + Sonarr.

Subcommando's:
  extract   safety-net: pak achtergebleven .rar/.zip/.7z uit in completed/
  import    push manualimport-kandidaten met zekere match door
  clean     verwijder stalled/failed queue-items + blocklist + optionele re-search
  all       extract -> import -> clean (cron-entry)
  anime     detecteer anime-series en zet seriesType/root/profiel (eigen cron)
  plexlang  zet in Plex de default audiotrack op de originele taal, alleen voor
            anime (eigen cron)

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
ANIME_LOCK_PATH = SCRIPT_DIR / ".lock-anime"
PLEX_LOCK_PATH = SCRIPT_DIR / ".lock-plex"
EXTRACT_MARKER_SUFFIX = ".extracted"
TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_ANIME_KEYWORD_ID = 210024  # TMDb keyword "anime"
TMDB_ANIMATION_GENRE_ID = 16    # TMDb genre "Animation"
# TMDb original_language (ISO-639-1) -> acceptabele Plex audio languageCodes
# (ISO-639-2). Plex gebruikt soms 'zho' en soms 'chi' voor Chinees.
ISO6391_TO_PLEX_LANG = {
    "ja": {"jpn"},
    "ko": {"kor"},
    "zh": {"zho", "chi", "cmn", "yue"},
}
# Een Plex-code -> alle codes die als dezelfde taal gelden (zho==chi==cmn==yue).
PLEX_LANG_EQUIV: dict[str, frozenset[str]] = {}
for _codes in ISO6391_TO_PLEX_LANG.values():
    for _c in _codes:
        PLEX_LANG_EQUIV[_c] = frozenset(_codes)
# Fallback wanneer een stream geen languageCode heeft, alleen een naam.
PLEX_LANGNAME_TO_CODE = {
    "japanese": "jpn", "korean": "kor", "chinese": "zho",
    "mandarin": "cmn", "cantonese": "yue",
}
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
    zombie_cleanup_enabled: bool
    max_zombie_remove_per_run: int
    tmdb_api_key: str
    anime_root_folder: str
    anime_quality_profile: str
    anime_detection: str
    anime_move_files: bool
    anime_refresh_after: bool
    max_anime_reclassify_per_run: int
    anime_exclude_ids: frozenset[int]
    plex_url: str
    plex_token: str
    plex_anime_sections: tuple[str, ...]
    plex_audio_fallback: tuple[str, ...]
    max_plex_audio_parts_per_run: int

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

    anime_detection = e.get("ANIME_DETECTION", "both").strip().lower()
    if anime_detection not in ("both", "tvdb", "tmdb"):
        sys.exit(f"FATAL: ANIME_DETECTION must be both|tvdb|tmdb, got {anime_detection!r}")
    exclude_ids: set[int] = set()
    for tok in (e.get("ANIME_EXCLUDE_IDS", "") or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok:
            try:
                exclude_ids.add(int(tok))
            except ValueError:
                log.warning("config: ignoring non-int ANIME_EXCLUDE_IDS value %r", tok)

    def as_tuple(k: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = (e.get(k, "") or "").replace(";", ",")
        vals = tuple(t.strip() for t in raw.split(",") if t.strip())
        return vals if vals else default

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
        zombie_cleanup_enabled=as_bool("ZOMBIE_CLEANUP_ENABLED", True),
        max_zombie_remove_per_run=as_int("MAX_ZOMBIE_REMOVE_PER_RUN", 100),
        tmdb_api_key=e.get("TMDB_API_KEY", "").strip(),
        anime_root_folder=e.get("ANIME_ROOT_FOLDER", "/anime-tv").strip().rstrip("/"),
        anime_quality_profile=e.get("ANIME_QUALITY_PROFILE", "Ultra-HD - Anime").strip(),
        anime_detection=anime_detection,
        anime_move_files=as_bool("ANIME_MOVE_FILES", True),
        anime_refresh_after=as_bool("ANIME_REFRESH_AFTER", True),
        max_anime_reclassify_per_run=as_int("MAX_ANIME_RECLASSIFY_PER_RUN", 25),
        anime_exclude_ids=frozenset(exclude_ids),
        plex_url=e.get("PLEX_URL", "").strip().rstrip("/"),
        plex_token=e.get("PLEX_TOKEN", "").strip(),
        plex_anime_sections=as_tuple("PLEX_ANIME_SECTIONS", ()),
        plex_audio_fallback=as_tuple("PLEX_AUDIO_FALLBACK", ("jpn", "kor", "zho")),
        max_plex_audio_parts_per_run=as_int("MAX_PLEX_AUDIO_PARTS_PER_RUN", 1000),
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


def acquire_lock(path: Path = LOCK_PATH):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
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


def arr_put(s: requests.Session, inst: ArrInstance, path: str, body, **params):
    headers = {"X-Api-Key": inst.api_key, "Content-Type": "application/json"}
    r = s.put(f"{inst.url}/api/v3{path}", headers=headers, json=body,
              params=params, timeout=60)
    r.raise_for_status()
    return r.json() if r.content else {}


def plex_get(s: requests.Session, cfg: Config, path: str, **params):
    headers = {"X-Plex-Token": cfg.plex_token, "Accept": "application/json"}
    r = s.get(f"{cfg.plex_url}{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


def plex_put(s: requests.Session, cfg: Config, path: str, **params):
    headers = {"X-Plex-Token": cfg.plex_token, "Accept": "application/json"}
    r = s.put(f"{cfg.plex_url}{path}", headers=headers, params=params, timeout=30)
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


def _is_unknown_rejection(rejections: list) -> bool:
    """True als rejection past bij 'Unknown Series/Movie' (parser-mismatch)."""
    for r in rejections or []:
        reason = (r.get("reason") or "").lower() if isinstance(r, dict) else str(r).lower()
        if "unknown series" in reason or "unknown movie" in reason:
            return True
    return False


def _resolve_episode_ids(inst: ArrInstance, s: requests.Session,
                         series_id: int, filename: str) -> list[int]:
    """Parse SxxExx uit filename en zoek bijbehorende episodeIds in Sonarr."""
    m = re.search(r"S(\d{1,2})E(\d{1,3})", filename, re.IGNORECASE)
    if not m:
        return []
    season, ep_num = int(m.group(1)), int(m.group(2))
    try:
        eps = arr_get(s, inst, "/episode", seriesId=series_id, seasonNumber=season)
    except requests.HTTPError as ex:
        log.warning("fuzzy[%s]: episode lookup failed seriesId=%s s%s: %s",
                    inst.name, series_id, season, ex)
        return []
    return [e["id"] for e in eps if e.get("episodeNumber") == ep_num]


def _try_fuzzy_match(inst: ArrInstance, s: requests.Session, cfg: Config,
                     skipped: list[dict]) -> list[dict]:
    """Return extra import-payloads voor items waar fuzzy-match werkt."""
    if not cfg.fuzzy_match_enabled or not skipped:
        return []
    # Filter alleen 'Unknown Series/Movie' rejections — andere rejections (upgrade,
    # sample-detection) zijn bewust en niet door parser-mismatch te verklaren.
    targets = [it for it in skipped if _is_unknown_rejection(it.get("rejections"))]
    if not targets:
        return []

    # Pool van bestaande titles ophalen (1× per run).
    pool: list[tuple[int, str]] = []
    if inst.is_sonarr:
        try:
            for sr in arr_get(s, inst, "/series"):
                pool.append((sr["id"], sr.get("title", "")))
                # alternateTitles vergroten match-kans
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
        match_id, score = _fuzzy_pick(fname, pool, cfg.fuzzy_match_threshold)
        if match_id is None:
            log.info("fuzzy[%s]: no match (best score %.2f) :: %s",
                     inst.name, score, fname)
            continue

        if inst.is_sonarr:
            ep_ids = _resolve_episode_ids(inst, s, match_id, fname)
            if not ep_ids:
                log.info("fuzzy[%s]: matched seriesId=%s (score %.2f) maar episodeId niet resolvable :: %s",
                         inst.name, match_id, score, fname)
                continue
            log.info("fuzzy[sonarr]: match seriesId=%s episodeIds=%s (score %.2f) :: %s",
                     match_id, ep_ids, score, fname)
            payloads.append({
                "path": path,
                "seriesId": match_id,
                "episodeIds": ep_ids,
                "quality": it.get("quality"),
                "languages": it.get("languages") or [{"id": 1, "name": "English"}],
                "downloadId": it.get("downloadId"),
                "episodeFileId": 0,
            })
        else:
            log.info("fuzzy[radarr]: match movieId=%s (score %.2f) :: %s",
                     match_id, score, fname)
            payloads.append({
                "path": path,
                "movieId": match_id,
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


def _zombie_rows_sonarr(rows: list[dict], episode_cache: dict[int, bool]) -> list[dict]:
    out = []
    for r in rows:
        if (r.get("status") or "").lower() != "completed":
            continue
        if (r.get("trackedDownloadState") or "").lower() not in ("importblocked", "importpending"):
            continue
        ep_id = r.get("episodeId")
        if ep_id and episode_cache.get(ep_id):
            out.append(r)
    return out


def _zombie_rows_radarr(rows: list[dict], movie_has_file: dict[int, bool]) -> list[dict]:
    out = []
    for r in rows:
        if (r.get("status") or "").lower() != "completed":
            continue
        if (r.get("trackedDownloadState") or "").lower() not in ("importblocked", "importpending"):
            continue
        mid = r.get("movieId")
        if mid and movie_has_file.get(mid):
            out.append(r)
    return out


def _cleanup_zombies(inst: ArrInstance, s: requests.Session, cfg: Config,
                     records: list[dict], dry_run: bool) -> list[dict]:
    """Verwijder queue-rows waarvan de file al in de library zit. Returns
    de overgebleven records (zonder zombies) zodat stalled-loop ze niet
    nogmaals oppakt."""
    if inst.is_sonarr:
        series_ids = {r["seriesId"] for r in records if r.get("seriesId")}
        ep_cache: dict[int, bool] = {}
        for sid in series_ids:
            try:
                for e in arr_get(s, inst, "/episode", seriesId=sid):
                    ep_cache[e["id"]] = bool(e.get("hasFile"))
            except requests.HTTPError as ex:
                log.warning("clean[%s]: episode cache fetch seriesId=%s failed: %s",
                            inst.name, sid, ex)
        zombies = _zombie_rows_sonarr(records, ep_cache)
    else:
        movie_ids = {r["movieId"] for r in records if r.get("movieId")}
        mf_cache: dict[int, bool] = {}
        for mid in movie_ids:
            try:
                mv = arr_get(s, inst, f"/movie/{mid}")
                mf_cache[mid] = bool(mv.get("hasFile"))
            except requests.HTTPError as ex:
                log.warning("clean[%s]: movie fetch id=%s failed: %s", inst.name, mid, ex)
        zombies = _zombie_rows_radarr(records, mf_cache)

    if not zombies:
        return records

    if len(zombies) > cfg.max_zombie_remove_per_run:
        log.warning("clean[%s]: %d zombies exceeds cap %d, capping",
                    inst.name, len(zombies), cfg.max_zombie_remove_per_run)
        zombies = zombies[:cfg.max_zombie_remove_per_run]

    log.info("clean[%s]: %d zombie row(s) (file already in library), removing without blocklist",
             inst.name, len(zombies))
    removed_ids: set[int] = set()
    for z in zombies:
        qid = z.get("id")
        title = (z.get("title") or "")[:80]
        if dry_run:
            log.info("[dry-run] clean[%s] zombie qid=%s title=%s", inst.name, qid, title)
            continue
        try:
            arr_delete(s, inst, f"/queue/{qid}",
                       removeFromClient="false", blocklist="false")
            removed_ids.add(qid)
            log.info("clean[%s]: zombie removed qid=%s title=%s", inst.name, qid, title)
        except requests.HTTPError as ex:
            log.error("clean[%s]: zombie delete qid=%s failed: %s", inst.name, qid, ex)
    log.info("clean[%s]: zombie summary removed=%d/%d",
             inst.name, len(removed_ids), len(zombies))

    zombie_qids = {z["id"] for z in zombies}
    return [r for r in records if r["id"] not in zombie_qids]


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

    if cfg.zombie_cleanup_enabled:
        records = _cleanup_zombies(inst, s, cfg, records, dry_run)
        if not records:
            return

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


# ---------- anime reclassify ----------

def _norm_profile(name: str) -> str:
    """Vergelijk profielnamen ongevoelig voor spaties/streepjes/case.
    'Ultra-HD - Anime' == 'Ultra-HD-Anime' == 'ultra hd anime'."""
    return re.sub(r"[\s_-]+", "", name or "").lower()


def _resolve_profile_id(s: requests.Session, inst: ArrInstance, name: str) -> int | None:
    target = _norm_profile(name)
    try:
        profiles = arr_get(s, inst, "/qualityprofile")
    except requests.HTTPError as ex:
        log.error("anime: qualityprofile lookup failed: %s", ex)
        return None
    for p in profiles:
        if _norm_profile(p.get("name", "")) == target:
            return p.get("id")
    log.error("anime: quality profile %r not found (have: %s)",
              name, ", ".join(p.get("name", "?") for p in profiles))
    return None


def _resolve_root_path(s: requests.Session, inst: ArrInstance, path: str) -> str | None:
    want = path.rstrip("/")
    try:
        roots = arr_get(s, inst, "/rootfolder")
    except requests.HTTPError as ex:
        log.error("anime: rootfolder lookup failed: %s", ex)
        return None
    for r in roots:
        if (r.get("path") or "").rstrip("/") == want:
            return r.get("path").rstrip("/")
    log.error("anime: root folder %r not configured in Sonarr (have: %s)",
              path, ", ".join((r.get("path") or "?") for r in roots))
    return None


def _tmdb_is_anime(s: requests.Session, api_key: str, tmdb_id: int,
                   cache: dict[int, bool]) -> bool:
    """Vraag TMDb of een tv-serie anime is: keyword 'anime' (210024) OF
    genre Animation (16) + origin_country bevat 'JP'. Resultaat per run gecached."""
    if tmdb_id in cache:
        return cache[tmdb_id]
    verdict = False
    try:
        kw = s.get(f"{TMDB_API_BASE}/tv/{tmdb_id}/keywords",
                   params={"api_key": api_key}, timeout=15)
        if kw.status_code == 200:
            for k in kw.json().get("results", []):
                if k.get("id") == TMDB_ANIME_KEYWORD_ID or \
                        (k.get("name", "").lower() == "anime"):
                    verdict = True
                    break
        elif kw.status_code in (401, 403):
            log.error("anime: TMDb auth failed (status %s) — check TMDB_API_KEY", kw.status_code)
        if not verdict:
            det = s.get(f"{TMDB_API_BASE}/tv/{tmdb_id}",
                        params={"api_key": api_key}, timeout=15)
            if det.status_code == 200:
                d = det.json()
                genre_ids = {g.get("id") for g in d.get("genres", [])}
                countries = set(d.get("origin_country") or [])
                if TMDB_ANIMATION_GENRE_ID in genre_ids and "JP" in countries:
                    verdict = True
    except requests.RequestException as ex:
        log.warning("anime: TMDb lookup failed for tmdbId=%s: %s", tmdb_id, ex)
    cache[tmdb_id] = verdict
    return verdict


def _is_anime(s: requests.Session, cfg: Config, series: dict,
              tmdb_cache: dict[int, bool]) -> bool:
    # 1. al handmatig/eerder als anime gemarkeerd — altijd waar.
    if (series.get("seriesType") or "").lower() == "anime":
        return True
    # 2. TheTVDB-genre (lokaal, gratis) — tenzij detectie puur tmdb is.
    if cfg.anime_detection in ("both", "tvdb"):
        if any((g or "").strip().lower() == "anime" for g in (series.get("genres") or [])):
            return True
    # 3. TMDb-lookup (alleen als sleutel + tmdbId aanwezig).
    if cfg.anime_detection in ("both", "tmdb") and cfg.tmdb_api_key:
        tmdb_id = series.get("tmdbId")
        if tmdb_id:
            return _tmdb_is_anime(s, cfg.tmdb_api_key, tmdb_id, tmdb_cache)
    return False


def cmd_anime(cfg: Config, dry_run: bool) -> None:
    s = make_session()
    inst = cfg.sonarr

    profile_id = _resolve_profile_id(s, inst, cfg.anime_quality_profile)
    root_path = _resolve_root_path(s, inst, cfg.anime_root_folder)
    if profile_id is None or root_path is None:
        log.error("anime: cannot resolve target profile/root, aborting reclassify")
        return

    if cfg.anime_detection in ("both", "tmdb") and not cfg.tmdb_api_key:
        log.warning("anime: TMDB_API_KEY empty — falling back to TheTVDB genre only")

    try:
        series_list = arr_get(s, inst, "/series")
    except requests.HTTPError as ex:
        log.error("anime: /series fetch failed: %s", ex)
        return

    tmdb_cache: dict[int, bool] = {}
    detected = 0
    to_change: list[dict] = []
    for sr in series_list:
        sid = sr.get("id")
        if sid in cfg.anime_exclude_ids:
            continue
        if not _is_anime(s, cfg, sr, tmdb_cache):
            continue
        detected += 1
        cur_type = (sr.get("seriesType") or "").lower()
        cur_root = (sr.get("rootFolderPath") or "").rstrip("/")
        cur_prof = sr.get("qualityProfileId")
        if cur_type == "anime" and cur_root == root_path and cur_prof == profile_id:
            continue  # al correct — idempotent skip
        to_change.append(sr)

    log.info("anime: %d/%d series detected as anime, %d need reclassify",
             detected, len(series_list), len(to_change))

    if len(to_change) > cfg.max_anime_reclassify_per_run:
        log.warning("anime: %d need changes, capping at MAX_ANIME_RECLASSIFY_PER_RUN=%d",
                    len(to_change), cfg.max_anime_reclassify_per_run)
        to_change = to_change[:cfg.max_anime_reclassify_per_run]

    for sr in to_change:
        sid = sr.get("id")
        title = sr.get("title") or sid
        cur_root = (sr.get("rootFolderPath") or "").rstrip("/")
        root_changes = cur_root != root_path
        move = cfg.anime_move_files and root_changes
        before = f"type={sr.get('seriesType')} root={cur_root} profile={sr.get('qualityProfileId')}"
        after = f"type=anime root={root_path} profile={profile_id}"
        if dry_run:
            log.info("[dry-run] anime reclassify sid=%s %s | %s -> %s | moveFiles=%s",
                     sid, title, before, after, move)
            continue
        try:
            arr_put(s, inst, "/series/editor", {
                "seriesIds": [sid],
                "seriesType": "anime",
                "qualityProfileId": profile_id,
                "rootFolderPath": root_path,
                "moveFiles": move,
            })
        except requests.HTTPError as ex:
            log.error("anime: editor PUT failed sid=%s %s: %s", sid, title, ex)
            continue
        log.info("anime: reclassified sid=%s %s | %s -> %s | moveFiles=%s",
                 sid, title, before, after, move)
        if cfg.anime_refresh_after:
            try:
                arr_post(s, inst, "/command", {"name": "RefreshSeries", "seriesId": sid})
            except requests.HTTPError as ex:
                log.warning("anime: RefreshSeries failed sid=%s: %s", sid, ex)


# ---------- plex audio language ----------

def _int_tail(guid: str) -> int:
    """Pak het eerste integer-id na '://' uit een Plex-guid."""
    m = re.search(r"://(\d+)", guid or "")
    return int(m.group(1)) if m else 0


def _plex_show_ids(show: dict) -> tuple[int, int]:
    """Return (tvdbId, tmdbId) uit Plex-show metadata. Ondersteunt zowel de
    nieuwe Guid-array (tvdb://, tmdb://) als het oude legacy `guid`-veld."""
    tvdb_id = tmdb_id = 0
    for g in (show.get("Guid") or []):
        gid = g.get("id") or ""
        if gid.startswith("tvdb://"):
            tvdb_id = _int_tail(gid)
        elif gid.startswith("tmdb://"):
            tmdb_id = _int_tail(gid)
    legacy = show.get("guid") or ""
    if not tvdb_id and "thetvdb" in legacy:
        tvdb_id = _int_tail(legacy)
    if not tmdb_id and ("themoviedb" in legacy or "tmdb" in legacy):
        tmdb_id = _int_tail(legacy)
    return tvdb_id, tmdb_id


def _tmdb_original_language(s: requests.Session, api_key: str, tmdb_id: int,
                           cache: dict[int, str | None]) -> str | None:
    """ISO-639-1 original_language voor een TMDb tv-serie. Per run gecached."""
    if tmdb_id in cache:
        return cache[tmdb_id]
    lang: str | None = None
    try:
        det = s.get(f"{TMDB_API_BASE}/tv/{tmdb_id}",
                    params={"api_key": api_key}, timeout=15)
        if det.status_code == 200:
            lang = (det.json().get("original_language") or "").lower() or None
        elif det.status_code in (401, 403):
            log.error("plexlang: TMDb auth failed (status %s) — check TMDB_API_KEY",
                      det.status_code)
    except requests.RequestException as ex:
        log.warning("plexlang: TMDb lookup failed tmdbId=%s: %s", tmdb_id, ex)
    cache[tmdb_id] = lang
    return lang


def _desired_pref(orig_iso6391: str | None,
                  fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Geordende lijst gewenste Plex audio-codes. TMDb-taal bekend -> die taal;
    anders de geconfigureerde fallback-volgorde."""
    if orig_iso6391:
        codes = ISO6391_TO_PLEX_LANG.get(orig_iso6391.lower())
        if codes:
            return tuple(sorted(codes))
    return fallback


def _truthy(v) -> bool:
    return v in (True, 1, "1") or (isinstance(v, str) and v.lower() == "true")


def _stream_code(st: dict) -> str:
    code = (st.get("languageCode") or "").lower()
    if code:
        return code
    return PLEX_LANGNAME_TO_CODE.get((st.get("language") or "").lower(), "")


def _pick_audio_stream(part: dict, pref_codes: tuple[str, ...]) -> dict | None:
    """Kies de audiostream die best bij de voorkeurstalen past (hoogste channels
    bij meerdere). None als geen enkele taal matcht."""
    audio = [st for st in (part.get("Stream") or [])
             if int(st.get("streamType", 0) or 0) == 2]
    for code in pref_codes:
        accept = PLEX_LANG_EQUIV.get(code, frozenset({code}))
        matches = [st for st in audio if _stream_code(st) in accept]
        if matches:
            return max(matches, key=lambda st: int(st.get("channels", 0) or 0))
    return None


def _ensure_streams(s: requests.Session, cfg: Config, ep: dict) -> list[dict]:
    """Geef Media[] van een aflevering terug met Stream-info. allLeaves bevat
    streams meestal al; zo niet, haal de detail-metadata op."""
    media = ep.get("Media") or []
    needs = any("Stream" not in p
                for m in media for p in (m.get("Part") or []))
    if not needs:
        return media
    rk = ep.get("ratingKey")
    try:
        detail = plex_get(s, cfg, f"/library/metadata/{rk}") \
            .get("MediaContainer", {}).get("Metadata", [])
        if detail:
            return detail[0].get("Media") or []
    except requests.HTTPError as ex:
        log.warning("plexlang: metadata fetch failed rk=%s: %s", rk, ex)
    return media


def cmd_plexlang(cfg: Config, dry_run: bool) -> None:
    if not cfg.plex_url or not cfg.plex_token:
        log.error("plexlang: PLEX_URL/PLEX_TOKEN not set in config.env — skipping")
        return
    s = make_session()
    inst = cfg.sonarr

    # 1. Anime-map uit Sonarr (seriesType=anime) — scope én taalbron.
    try:
        series_list = arr_get(s, inst, "/series")
    except requests.HTTPError as ex:
        log.error("plexlang: /series fetch failed: %s", ex)
        return
    anime_by_tvdb: dict[int, dict] = {}
    anime_by_tmdb: dict[int, dict] = {}
    for sr in series_list:
        if (sr.get("seriesType") or "").lower() != "anime":
            continue
        entry = {"title": sr.get("title"), "tmdbId": int(sr.get("tmdbId") or 0)}
        if sr.get("tvdbId"):
            anime_by_tvdb[int(sr["tvdbId"])] = entry
        if sr.get("tmdbId"):
            anime_by_tmdb[int(sr["tmdbId"])] = entry
    if not anime_by_tvdb and not anime_by_tmdb:
        log.info("plexlang: no Sonarr series with seriesType=anime — nothing to do")
        return
    log.info("plexlang: anime series in Sonarr: %d with tvdbId, %d with tmdbId",
             len(anime_by_tvdb), len(anime_by_tmdb))

    # 2. Plex show-secties (optioneel op naam gefilterd).
    try:
        sections = plex_get(s, cfg, "/library/sections") \
            .get("MediaContainer", {}).get("Directory", [])
    except requests.HTTPError as ex:
        log.error("plexlang: /library/sections failed: %s", ex)
        return
    show_sections = [d for d in sections if d.get("type") == "show"]
    if cfg.plex_anime_sections:
        wanted = {n.lower() for n in cfg.plex_anime_sections}
        show_sections = [d for d in show_sections
                         if (d.get("title") or "").lower() in wanted]
    if not show_sections:
        log.warning("plexlang: no matching Plex show sections (filter=%s)",
                    list(cfg.plex_anime_sections) or "all")
        return

    tmdb_lang_cache: dict[int, str | None] = {}
    changed = 0
    capped = False

    for sec in show_sections:
        if capped:
            break
        key = sec.get("key")
        try:
            shows = plex_get(s, cfg, f"/library/sections/{key}/all", type=2) \
                .get("MediaContainer", {}).get("Metadata", [])
        except requests.HTTPError as ex:
            log.error("plexlang: section %s listing failed: %s", key, ex)
            continue

        for show in shows:
            if capped:
                break
            tvdb_id, tmdb_id = _plex_show_ids(show)
            entry = anime_by_tvdb.get(tvdb_id) or anime_by_tmdb.get(tmdb_id)
            if entry is None:
                log.debug("plexlang: skip unmatched/non-anime %r (tvdb=%s tmdb=%s)",
                          show.get("title"), tvdb_id, tmdb_id)
                continue

            tmdb_for_lang = entry.get("tmdbId") or tmdb_id
            orig = None
            if cfg.tmdb_api_key and tmdb_for_lang:
                orig = _tmdb_original_language(s, cfg.tmdb_api_key,
                                               int(tmdb_for_lang), tmdb_lang_cache)
            pref = _desired_pref(orig, cfg.plex_audio_fallback)
            log.debug("plexlang: anime %r -> orig=%s pref=%s",
                      show.get("title"), orig, pref)

            rk = show.get("ratingKey")
            try:
                leaves = plex_get(s, cfg, f"/library/metadata/{rk}/allLeaves") \
                    .get("MediaContainer", {}).get("Metadata", [])
            except requests.HTTPError as ex:
                log.error("plexlang: allLeaves failed for %r: %s",
                          show.get("title"), ex)
                continue

            for ep in leaves:
                if capped:
                    break
                ep_label = f"{show.get('title')} S{ep.get('parentIndex')}E{ep.get('index')}"
                for media in _ensure_streams(s, cfg, ep):
                    if capped:
                        break
                    for part in (media.get("Part") or []):
                        st = _pick_audio_stream(part, pref)
                        if st is None:
                            log.debug("plexlang: no %s audio :: %s", pref, ep_label)
                            continue
                        if _truthy(st.get("selected")):
                            continue  # idempotent: al de default
                        if changed >= cfg.max_plex_audio_parts_per_run:
                            log.warning("plexlang: reached cap %d, stopping",
                                        cfg.max_plex_audio_parts_per_run)
                            capped = True
                            break
                        code = _stream_code(st)
                        if dry_run:
                            log.info("[dry-run] plexlang set audio id=%s (%s) :: %s",
                                     st.get("id"), code, ep_label)
                            changed += 1
                            continue
                        try:
                            plex_put(s, cfg, f"/library/parts/{part.get('id')}",
                                     audioStreamID=st.get("id"))
                            changed += 1
                            log.info("plexlang: set audio id=%s (%s) :: %s",
                                     st.get("id"), code, ep_label)
                        except requests.HTTPError as ex:
                            log.error("plexlang: PUT part %s failed: %s",
                                      part.get("id"), ex)

    log.info("plexlang: done, %d part(s) %s", changed,
             "would change (dry-run)" if dry_run else "changed")


# ---------- entry ----------

def main() -> int:
    p = argparse.ArgumentParser(description="arr-janitor")
    p.add_argument("command",
                   choices=["extract", "import", "clean", "all", "anime", "plexlang"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    setup_logging(args.verbose)
    cfg = load_config()

    # 'anime' en 'plexlang' krijgen elk een eigen lock zodat ze nooit botsen
    # met de 5x/nacht extract/import/clean-cyclus (en met elkaar).
    lock_path = {
        "anime": ANIME_LOCK_PATH,
        "plexlang": PLEX_LOCK_PATH,
    }.get(args.command, LOCK_PATH)
    try:
        lock_fd = acquire_lock(lock_path)
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
        if args.command == "anime":
            cmd_anime(cfg, args.dry_run)
        if args.command == "plexlang":
            cmd_plexlang(cfg, args.dry_run)
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
