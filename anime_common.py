#!/usr/bin/env python3
"""Gedeelde helpers voor de anime-scripts (anime_sort.py, anime_audio.py).

Config uit SECRETS_FILE (default de fleet-secrets, zelfde bron als de rest van
de media-fleet); machine-specifieke paden via machine.env naast dit script
(gegenereerd door setup.sh, gitignored). Bevat: HTTP-sessie met retry,
Radarr/Sonarr/Emby-clients, Telegram, fcntl-lockfile, logging,
container<->host padmapping en taalnaam->ISO-639 mapping.
"""
from __future__ import annotations

import fcntl
import logging
import logging.handlers
import os
import socket
import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    sys.exit("FATAL: python-dotenv ontbreekt")

# Machine-specifieke instellingen. Defaults = TheBeastServer; override per
# machine via machine.env (gitignored, gegenereerd door setup.sh) of gewone
# env-vars. Volgorde is bewust: machine.env EERST, daarna pas de secrets —
# load_dotenv overschrijft reeds gezette env-vars niet, dus machine.env wint
# van de secrets-file en echte env-vars (cron) winnen van alles.
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / "machine.env")

SECRETS_PATH = os.getenv("SECRETS_FILE", "/home/sven/scripts/secrets/config.env")
LOG_DIR = Path(os.getenv("LOG_DIR", "/home/sven/scripts/logs"))
HOST_LABEL = os.getenv("HOST_LABEL", socket.gethostname())

load_dotenv(SECRETS_PATH)

RADARR_URL = os.getenv("RADARR_URL", "http://localhost:7878").rstrip("/")
RADARR_API_KEY = os.getenv("RADARR_API_KEY")
SONARR_URL = os.getenv("SONARR_URL", "http://localhost:8989").rstrip("/")
SONARR_API_KEY = os.getenv("SONARR_API_KEY")
EMBY_URL = os.getenv("EMBY_URL", "http://localhost:8096").rstrip("/")
EMBY_API_KEY = os.getenv("EMBY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Anime-talen voor classificatie (script 2). Override via ANIME_LANGS in config.env.
ANIME_LANGS = {
    s.strip().lower()
    for s in os.getenv("ANIME_LANGS", "Japanese,Chinese,Korean").split(",")
    if s.strip()
}

# Basis van de host-mediamappen; per map overridebaar via env (machine.env).
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/home/sven/media"))


def _media_dir(env_key: str, default_subdir: str) -> Path:
    """Host-mediamap: MEDIA_ROOT/<subdir>, individueel overridebaar via env."""
    return Path(os.getenv(env_key, str(MEDIA_ROOT / default_subdir)))


# Container-root (zoals *arr het pad rapporteert) -> host-pad. Let op de casing:
# container is lowercase, host-map is met hoofdletters. De container-keys zijn
# vast (bepaald door de *arr container-mounts).
RADARR_ROOT_HOST = {
    "/movies": _media_dir("MOVIES_DIR", "movies"),
    "/nl-movies": _media_dir("NL_MOVIES_DIR", "NL-movies"),
    "/anime-movies": _media_dir("ANIME_MOVIES_DIR", "Anime-movies"),
}
SONARR_ROOT_HOST = {
    "/tv": _media_dir("TV_DIR", "tv"),
    "/nl-tv": _media_dir("NL_TV_DIR", "NL-tv"),
    "/anime-tv": _media_dir("ANIME_TV_DIR", "Anime-tv"),
}
ANIME_MOVIES_HOST = RADARR_ROOT_HOST["/anime-movies"]
ANIME_TV_HOST = SONARR_ROOT_HOST["/anime-tv"]

# TVDB/TMDB taalnaam -> set ISO-639 codes zoals ffprobe ze in tags.language zet.
LANG_CODES: dict[str, set[str]] = {
    "japanese": {"jpn", "ja", "jp"},
    "chinese": {"chi", "zho", "zh", "cmn", "yue", "chs", "cht", "zh-hans", "zh-hant"},
    "korean": {"kor", "ko"},
    "english": {"eng", "en"},
    "french": {"fre", "fra", "fr"},
    "german": {"ger", "deu", "de"},
    "spanish": {"spa", "es"},
    "italian": {"ita", "it"},
    "russian": {"rus", "ru"},
    "thai": {"tha", "th"},
    "dutch": {"dut", "nld", "nl"},
    "portuguese": {"por", "pt"},
    "hindi": {"hin", "hi"},
    "indonesian": {"ind", "id"},
    "tagalog": {"tgl", "tl"},
    "turkish": {"tur", "tr"},
    "polish": {"pol", "pl"},
    "norwegian": {"nor", "no", "nob", "nno"},
    "finnish": {"fin", "fi"},
    "swedish": {"swe", "sv"},
    "malayalam": {"mal", "ml"},
    "vietnamese": {"vie", "vi"},
}


def lang_to_codes(name: str | None) -> set[str]:
    """Map een TVDB-taalnaam naar ISO-639 codes; lege set als onbekend."""
    if not name:
        return set()
    return LANG_CODES.get(name.strip().lower(), set())


# ---------- logging + lock ----------

def setup_logging(name: str, verbose: bool) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


class LockHeld(Exception):
    pass


def acquire_lock(name: str) -> int:
    """fcntl flock op /tmp/<name>.lock. Raise LockHeld als al actief."""
    fd = os.open(f"/tmp/{name}.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise LockHeld()
    return fd


def release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


# ---------- HTTP ----------

def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
                  raise_on_status=False)
    a = HTTPAdapter(max_retries=retry)
    s.mount("http://", a)
    s.mount("https://", a)
    return s


SESSION = _make_session()


def _arr_target(which: str) -> tuple[str, str]:
    if which == "radarr":
        return RADARR_URL, RADARR_API_KEY or ""
    return SONARR_URL, SONARR_API_KEY or ""


def arr_get(which: str, path: str, **params):
    url, key = _arr_target(which)
    r = SESSION.get(f"{url}/api/v3{path}", headers={"X-Api-Key": key},
                    params=params, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else None


def arr_put(which: str, path: str, body, **params):
    url, key = _arr_target(which)
    r = SESSION.put(f"{url}/api/v3{path}",
                    headers={"X-Api-Key": key, "Content-Type": "application/json"},
                    json=body, params=params, timeout=120)
    r.raise_for_status()
    return r.json() if r.content else {}


def arr_post(which: str, path: str, body, **params):
    url, key = _arr_target(which)
    r = SESSION.post(f"{url}/api/v3{path}",
                     headers={"X-Api-Key": key, "Content-Type": "application/json"},
                     json=body, params=params, timeout=120)
    r.raise_for_status()
    return r.json() if r.content else {}


def arr_delete(which: str, path: str, **params):
    url, key = _arr_target(which)
    r = SESSION.delete(f"{url}/api/v3{path}", headers={"X-Api-Key": key},
                       params=params, timeout=60)
    r.raise_for_status()
    return r.json() if r.content else None


def resolve_rootfolder(which: str, container_path: str) -> str | None:
    """Geef het exacte rootFolderPath terug zoals *arr het kent."""
    for rf in arr_get(which, "/rootfolder") or []:
        if (rf.get("path") or "").rstrip("/").lower() == container_path.rstrip("/").lower():
            return rf["path"].rstrip("/")
    return None


def resolve_quality_profile(which: str, name_contains: str) -> tuple[int | None, str | None]:
    """Vind quality-profiel-id waarvan de naam name_contains bevat (case-insensitive)."""
    want = name_contains.lower()
    for qp in arr_get(which, "/qualityprofile") or []:
        if want in (qp.get("name") or "").lower():
            return qp["id"], qp.get("name")
    return None, None


# ---------- Emby ----------

def emby_get(endpoint: str, **params):
    p = dict(params)
    p["api_key"] = EMBY_API_KEY
    r = SESSION.get(f"{EMBY_URL}/{endpoint.lstrip('/')}", params=p, timeout=60)
    r.raise_for_status()
    return r.json() if r.content else None


def emby_post(endpoint: str, body=None, **params):
    p = dict(params)
    p["api_key"] = EMBY_API_KEY
    r = SESSION.post(f"{EMBY_URL}/{endpoint.lstrip('/')}", params=p, json=body, timeout=60)
    r.raise_for_status()
    return r


def emby_refresh_library(log: logging.Logger | None = None) -> None:
    """Best-effort volledige Emby library-rescan (pakt gewijzigde streams + verplaatste items)."""
    try:
        emby_post("Library/Refresh")
        if log:
            log.info("emby: library refresh getriggerd")
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("emby: refresh mislukt: %s", e)


# ---------- padmapping ----------

def container_to_host(container_path: str | None, root_map: dict[str, Path]) -> Path | None:
    if not container_path:
        return None
    cp = container_path.rstrip("/")
    for cprefix, hpath in root_map.items():
        if cp == cprefix or cp.startswith(cprefix + "/"):
            rel = cp[len(cprefix):].lstrip("/")
            return hpath / rel if rel else hpath
    return None


# ---------- Telegram ----------

def send_telegram(message: str, log: logging.Logger | None = None) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        if log:
            log.warning("Telegram-config ontbreekt, notificatie overgeslagen")
        return
    try:
        SESSION.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "parse_mode": "HTML", "text": message},
            timeout=15)
    except Exception as e:  # noqa: BLE001
        if log:
            log.error("Telegram mislukt: %s", e)


def require_keys(*, emby: bool = False) -> list[str]:
    missing = []
    if not RADARR_API_KEY:
        missing.append("RADARR_API_KEY")
    if not SONARR_API_KEY:
        missing.append("SONARR_API_KEY")
    if emby and not EMBY_API_KEY:
        missing.append("EMBY_API_KEY")
    return missing
