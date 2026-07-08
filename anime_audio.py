#!/usr/bin/env python3
"""anime_audio.py — zet per anime-bestand de ORIGINELE taal als default audiotrack.

Per titel komt de originele taal uit TVDB (originalLanguage van Sonarr/Radarr) — dus
Japans, Chinees, Koreaans, of welke taal dan ook; niet hardcoded. De audiotrack in die
taal krijgt de default-track-flag, de overige audiotracks verliezen 'm. Zo speelt elke
speler (Emby, Plex) en elke gebruiker standaard de originele audio.

  .mkv -> mkvpropedit (in-place header-edit, geen re-encode)
  .mp4 -> ffmpeg -c copy remux naar temp + atomische replace

Idempotent (bestanden die al goed staan worden overgeslagen) en met een state-cache
(pad+mtime) zodat ongewijzigde bestanden niet elke nacht opnieuw ge-ffprobed worden.
Titel niet in *arr, of geen audiotrack in de originele taal -> overslaan + loggen.

Flags: --dry-run, --verbose, --no-cache
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

import anime_common as ac

STATE_PATH = Path(__file__).resolve().parent / "anime_audio_state.json"
MAX_CHANGES = int(os.getenv("ANIME_AUDIO_MAX_PER_RUN", "1000"))
VIDEO_EXTS = {".mkv", ".mp4", ".m4v"}


# ---------- originele-taal map uit *arr (TVDB) ----------

def build_lang_map(log) -> tuple[dict[str, str], dict[str, str]]:
    """Geef (by_path, by_name): host-title-dir -> originalLanguage, en basename -> taal."""
    by_path: dict[str, str] = {}
    by_name: dict[str, str] = {}

    def add(host: Path | None, lang: str | None):
        if host and lang:
            by_path[str(host)] = lang
            by_name.setdefault(host.name, lang)

    try:
        for m in ac.arr_get("radarr", "/movie") or []:
            if "anime" in (m.get("rootFolderPath") or "").lower():
                add(ac.container_to_host(m.get("path"), ac.RADARR_ROOT_HOST),
                    (m.get("originalLanguage") or {}).get("name"))
    except requests.HTTPError as e:
        log.warning("radarr /movie ophalen mislukt: %s", e)
    try:
        for s in ac.arr_get("sonarr", "/series") or []:
            if "anime" in (s.get("rootFolderPath") or "").lower():
                add(ac.container_to_host(s.get("path"), ac.SONARR_ROOT_HOST),
                    (s.get("originalLanguage") or {}).get("name"))
    except requests.HTTPError as e:
        log.warning("sonarr /series ophalen mislukt: %s", e)
    return by_path, by_name


# ---------- ffprobe ----------

def ffprobe_audio(path: Path) -> list[dict]:
    """Lijst audiostreams op volgorde: {pos, lang, channels, default}."""
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-300:].decode("utf-8", "replace"))
    data = json.loads(out.stdout or b"{}")
    audio, pos = [], 0
    for st in data.get("streams", []):
        if st.get("codec_type") != "audio":
            continue
        lang = ((st.get("tags") or {}).get("language") or "").lower()
        audio.append({
            "pos": pos,
            "lang": lang,
            "channels": st.get("channels") or 0,
            "default": bool((st.get("disposition") or {}).get("default")),
        })
        pos += 1
    return audio


def decide_target(audio: list[dict], desired: set[str]) -> tuple[str, dict | None]:
    """Bepaal of/welke audiotrack default moet worden.

    Returns (status, target):
      'ok'     -> speelt al de originele taal af (precies één default, in de juiste taal)
      'nolang' -> geen audiotrack in de originele taal aanwezig
      'change' -> 'target' moet de default worden (overige audio default uit)

    Bewust géén churn: als de huidige default al de originele taal is, niets doen
    (ook niet 'upgraden' naar een andere same-language track met meer kanalen).
    """
    defaults = [a for a in audio if a["default"]]
    if len(defaults) == 1 and defaults[0]["lang"] in desired:
        return ("ok", None)
    cands = [a for a in audio if a["lang"] in desired]
    if not cands:
        return ("nolang", None)
    cands.sort(key=lambda a: (-a["channels"], a["pos"]))
    return ("change", cands[0])


# ---------- default-flag zetten ----------

def set_default_mkv(path: Path, num_audio: int, target_pos: int) -> None:
    cmd = ["mkvpropedit", str(path)]
    for i in range(num_audio):
        cmd += ["--edit", f"track:a{i + 1}", "--set",
                f"flag-default={'1' if i == target_pos else '0'}"]
    out = subprocess.run(cmd, capture_output=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-300:].decode("utf-8", "replace")
                           or out.stdout[-300:].decode("utf-8", "replace"))


def set_default_mp4(path: Path, num_audio: int, target_pos: int) -> None:
    fd, tmp_name = tempfile.mkstemp(suffix=".mp4", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-map", "0", "-c", "copy"]
        for i in range(num_audio):
            cmd += [f"-disposition:a:{i}", "default" if i == target_pos else "0"]
        cmd += [str(tmp)]
        out = subprocess.run(cmd, capture_output=True, timeout=1800)
        if out.returncode != 0:
            raise RuntimeError(out.stderr[-300:].decode("utf-8", "replace"))
        # Verifieer dat de juiste track nu default is.
        verify = ffprobe_audio(tmp)
        if len(verify) <= target_pos or not verify[target_pos]["default"]:
            raise RuntimeError("verificatie mislukt: target track niet default in output")
        # Rechten/eigenaar overnemen (mtime NIET — die moet wijzigen voor Emby + cache).
        st = path.stat()
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------- state cache ----------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_PATH)


# ---------- hoofdloop ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="anime-audio: originele taal als default audiotrack")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-cache", action="store_true", help="negeer de state-cache")
    args = ap.parse_args()

    log = ac.setup_logging("anime_audio", args.verbose)
    missing = ac.require_keys(emby=True)
    if missing:
        log.error("config.env mist keys: %s", missing)
        return 1

    have_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    have_mkv = bool(shutil.which("mkvpropedit"))
    if not have_ffmpeg:
        log.error("ffmpeg/ffprobe ontbreekt — kan niets doen")
        return 1
    if not have_mkv:
        log.warning("mkvpropedit ontbreekt (installeer mkvtoolnix) — .mkv wordt overgeslagen")

    try:
        lock = ac.acquire_lock("anime_audio")
    except ac.LockHeld:
        log.info("vorige run nog actief, exit")
        return 0

    log.info("=== anime_audio start (dry_run=%s, cache=%s) ===",
             args.dry_run, "uit" if args.no_cache else "aan")
    state = {} if args.no_cache else load_state()
    c = {k: 0 for k in ("seen", "cached", "unmatched", "unknown_lang", "no_audio",
                        "no_lang_track", "already_ok", "changed", "would_change",
                        "no_tool", "error")}
    changed_titles: set[str] = set()
    rc = 0
    try:
        by_path, by_name = build_lang_map(log)
        log.info("originele-taal map: %d titels uit *arr", len(by_path))
        budget = MAX_CHANGES
        capped = False
        for root in (ac.ANIME_MOVIES_HOST, ac.ANIME_TV_HOST):
            if capped:
                break
            if not root.exists():
                log.warning("root ontbreekt: %s", root)
                continue
            for f in sorted(root.rglob("*")):
                if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                    continue
                c["seen"] += 1
                rel = f.relative_to(root)
                title_dir = root / rel.parts[0]
                tname = title_dir.name

                lang = by_path.get(str(title_dir)) or by_name.get(tname)
                if not lang:
                    c["unmatched"] += 1
                    log.debug("unmatched (geen *arr-titel): %s", f)
                    continue
                desired = ac.lang_to_codes(lang)
                if not desired:
                    c["unknown_lang"] += 1
                    log.info("onbekende taal-mapping %r voor %s :: %s", lang, tname, f.name)
                    continue

                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                key = str(f)
                if not args.no_cache and state.get(key) == mtime:
                    c["cached"] += 1
                    continue

                try:
                    audio = ffprobe_audio(f)
                except Exception as e:  # noqa: BLE001
                    c["error"] += 1
                    log.warning("ffprobe faalde %s: %s", f.name, e)
                    continue
                if not audio:
                    c["no_audio"] += 1
                    state[key] = mtime
                    continue

                status, target = decide_target(audio, desired)
                if status == "nolang":
                    c["no_lang_track"] += 1
                    log.info("geen %s-audio (%s) :: %s", lang, sorted(desired), f.name)
                    state[key] = mtime
                    continue
                if status == "ok":
                    c["already_ok"] += 1
                    state[key] = mtime
                    continue

                is_mkv = f.suffix.lower() == ".mkv"
                if is_mkv and not have_mkv:
                    c["no_tool"] += 1
                    continue

                if args.dry_run:
                    c["would_change"] += 1
                    changed_titles.add(tname)
                    log.info("[dry-run] %s -> default audio = %s (pos %d, %dch) :: %s",
                             f.name, lang, target["pos"], target["channels"], tname)
                    continue

                if budget <= 0:
                    log.warning("cap %d wijzigingen bereikt, rest volgt volgende run", MAX_CHANGES)
                    capped = True
                    break

                try:
                    if is_mkv:
                        set_default_mkv(f, len(audio), target["pos"])
                    else:
                        set_default_mp4(f, len(audio), target["pos"])
                    state[key] = f.stat().st_mtime  # nieuwe mtime na edit
                    c["changed"] += 1
                    budget -= 1
                    changed_titles.add(tname)
                    log.info("gefixt -> %s default :: %s", lang, f.name)
                except Exception as e:  # noqa: BLE001
                    c["error"] += 1
                    log.error("fix faalde %s: %s", f.name, e)

        log.info("=== klaar: %s ===", c)
        if not args.dry_run and c["changed"] and ac.EMBY_API_KEY:
            ac.emby_refresh_library(log)
        _telegram(args.dry_run, c, sorted(changed_titles), log)
    except Exception:  # noqa: BLE001
        log.exception("onverwachte fout")
        ac.send_telegram(f"<b>{ac.HOST_LABEL}</b>\n<b>anime-audio — FOUT</b>\nZie log.", log)
        rc = 1
    finally:
        if not args.dry_run:
            try:
                save_state(state)
            except Exception as e:  # noqa: BLE001
                log.error("state opslaan mislukt: %s", e)
        ac.release_lock(lock)
    return rc


def _telegram(dry_run, c, titles, log):
    mode = " (DRY-RUN)" if dry_run else ""
    n = c["would_change"] if dry_run else c["changed"]
    if not n:
        ac.send_telegram(
            f"<b>{ac.HOST_LABEL}</b>\n<b>anime-audio{mode}</b>\n\n"
            f"Niets te wijzigen ({c['already_ok']} al goed, {c['no_lang_track']} zonder "
            f"originele audio, {c['unmatched']} niet in *arr).", log)
        return
    verb = "zou fixen" if dry_run else "gefixt"
    msg = [f"<b>{ac.HOST_LABEL}</b>", f"<b>anime-audio{mode}</b>", "",
           f"<b>{n} bestand(en) {verb}</b> over {len(titles)} titel(s):"]
    msg += [f"  • {t}" for t in titles[:15]]
    if len(titles) > 15:
        msg.append(f"  … +{len(titles) - 15}")
    msg.append("")
    msg.append(f"al goed: {c['already_ok']} · geen orig. audio: {c['no_lang_track']} · "
               f"niet in *arr: {c['unmatched']} · fouten: {c['error']}")
    ac.send_telegram("\n".join(msg), log)


if __name__ == "__main__":
    sys.exit(main())
