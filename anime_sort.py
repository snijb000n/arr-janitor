#!/usr/bin/env python3
"""anime_sort.py — verplaats anime naar de juiste *arr-root + zet profielen goed.

Detectie (origine uit TVDB via originalLanguage + genre):
  Radarr film = anime  als genre Animation/Anime bevat EN originalLanguage in ANIME_LANGS
  Sonarr serie = anime als seriesType=='anime' OF
                 (genre Animation/Anime bevat EN originalLanguage in ANIME_LANGS)

Verplaatsen gebeurt via de editor-endpoints met moveFiles=true, zodat *arr de
bestanden fysiek verplaatst én zijn database consistent bijwerkt. Voor films wordt
het Anime-quality-profiel gezet; voor series ook seriesType=anime, behalve voor
Chinese/Koreaanse 'standard'-donghua (die laten we standard om de afleverings-
nummering niet te breken).

Veiligheid: bestaat de doelmap in de anime-root al mét videobestanden (een
bestaande dubbele kopie), dan slaan we die titel over met een waarschuwing — geen
risico op merge/overschrijven. Eén richting: alleen NAAR de anime-root.

Flags: --dry-run, --verbose
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

import anime_common as ac

MAX_MOVES = int(os.getenv("ANIME_MAX_MOVES_PER_RUN", "20"))
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi"}
_ANIME_GENRES = {"anime", "animation"}


def _orig_lang(item: dict) -> str:
    return ((item.get("originalLanguage") or {}).get("name") or "").lower()


def _has_anime_genre(item: dict) -> bool:
    return bool(_ANIME_GENRES & {g.lower() for g in (item.get("genres") or [])})


def is_anime_movie(m: dict) -> bool:
    return _has_anime_genre(m) and _orig_lang(m) in ac.ANIME_LANGS


def is_anime_series(s: dict) -> bool:
    if (s.get("seriesType") or "").lower() == "anime":
        return True
    return _has_anime_genre(s) and _orig_lang(s) in ac.ANIME_LANGS


def _in_anime_root(item: dict) -> bool:
    return "anime" in (item.get("rootFolderPath") or "").lower()


def _dest_has_video(dest: Path) -> bool:
    """True als doelmap al bestaat en videobestanden bevat (bestaande dubbele kopie)."""
    if not dest.exists():
        return False
    return any(p.is_file() and p.suffix.lower() in VIDEO_EXTS for p in dest.rglob("*"))


def move_movies(cands, root, host_root, qp, dry_run, log, budget):
    moved, names, conflicts = 0, [], []
    for m in cands:
        title = f"{m.get('title')} ({m.get('year')})"
        dest = host_root / Path(m.get("path") or "").name
        if _dest_has_video(dest):
            log.warning("radarr CONFLICT (doelmap bestaat al met bestanden), overslaan :: %s -> %s",
                        title, dest)
            conflicts.append(title)
            continue
        if budget[0] <= 0:
            log.warning("radarr: cap %d bereikt, rest volgt volgende run", MAX_MOVES)
            break
        reason = f"genres={m.get('genres')} lang={_orig_lang(m)}"
        if dry_run:
            log.info("[dry-run] radarr move -> %s (qp=%s) :: %s [%s]", root, qp, title, reason)
            moved += 1
            budget[0] -= 1
            names.append(title)
            continue
        try:
            ac.arr_put("radarr", "/movie/editor",
                       {"movieIds": [m["id"]], "rootFolderPath": root,
                        "qualityProfileId": qp, "moveFiles": True})
            log.info("radarr moved -> %s :: %s", root, title)
            moved += 1
            budget[0] -= 1
            names.append(title)
        except requests.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else ""
            log.error("radarr move faalde %s: %s %s", title, e, body)
    return moved, names, conflicts


def move_series(cands, root, host_root, qp, dry_run, log, budget):
    moved, names, conflicts = 0, [], []
    for s in cands:
        title = s.get("title")
        dest = host_root / Path(s.get("path") or "").name
        if _dest_has_video(dest):
            log.warning("sonarr CONFLICT (doelmap bestaat al met bestanden), overslaan :: %s -> %s",
                        title, dest)
            conflicts.append(title)
            continue
        if budget[0] <= 0:
            log.warning("sonarr: cap %d bereikt, rest volgt volgende run", MAX_MOVES)
            break
        cur_type = (s.get("seriesType") or "").lower()
        # seriesType=anime alleen bij Japanse of al-anime series. Chinese/Koreaanse
        # donghua die 'standard' zijn laten we standard (anime-nummering kan ze breken),
        # net zoals de bestaande donghua al in /anime-tv staan.
        set_anime_type = cur_type == "anime" or _orig_lang(s) == "japanese"
        type_note = "seriesType=anime" if set_anime_type else f"seriesType={cur_type} (ongemoeid)"
        reason = f"type={cur_type} genres={s.get('genres')} lang={_orig_lang(s)}"
        if dry_run:
            log.info("[dry-run] sonarr move -> %s (qp=%s, %s) :: %s [%s]",
                     root, qp, type_note, title, reason)
            moved += 1
            budget[0] -= 1
            names.append(title)
            continue
        body = {"seriesIds": [s["id"]], "rootFolderPath": root,
                "qualityProfileId": qp, "moveFiles": True}
        if set_anime_type:
            body["seriesType"] = "anime"
        try:
            ac.arr_put("sonarr", "/series/editor", body)
            log.info("sonarr moved -> %s (%s) :: %s", root, type_note, title)
            moved += 1
            budget[0] -= 1
            names.append(title)
        except requests.HTTPError as e:
            body_txt = e.response.text[:300] if e.response is not None else ""
            log.error("sonarr move faalde %s: %s %s", title, e, body_txt)
    return moved, names, conflicts


def _telegram(dry_run, mv_names, sr_names, conflicts, log):
    mode = " (DRY-RUN)" if dry_run else ""
    verb = "zou verplaatsen" if dry_run else "verplaatst"
    if not mv_names and not sr_names and not conflicts:
        ac.send_telegram(
            f"<b>{ac.HOST_LABEL}</b>\n<b>anime-sort{mode}</b>\n\nNiets te verplaatsen.", log)
        return
    msg = [f"<b>{ac.HOST_LABEL}</b>", f"<b>anime-sort{mode}</b>", ""]
    if mv_names:
        msg.append(f"<b>Films {verb} ({len(mv_names)}):</b>")
        msg += [f"  • {n}" for n in mv_names[:15]]
        if len(mv_names) > 15:
            msg.append(f"  … +{len(mv_names) - 15}")
    if sr_names:
        msg.append(f"<b>Series {verb} ({len(sr_names)}):</b>")
        msg += [f"  • {n}" for n in sr_names[:15]]
        if len(sr_names) > 15:
            msg.append(f"  … +{len(sr_names) - 15}")
    if conflicts:
        msg.append(f"<b>⚠ Overgeslagen — dubbele map ({len(conflicts)}):</b>")
        msg += [f"  • {n}" for n in conflicts[:15]]
        if len(conflicts) > 15:
            msg.append(f"  … +{len(conflicts) - 15}")
        msg.append("(handmatig opschonen welke kopie blijft)")
    ac.send_telegram("\n".join(msg), log)


def main() -> int:
    ap = argparse.ArgumentParser(description="anime-sort: verplaats anime naar juiste root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    log = ac.setup_logging("anime_sort", args.verbose)
    missing = ac.require_keys()
    if missing:
        log.error("config.env mist keys: %s", missing)
        return 1

    try:
        lock = ac.acquire_lock("anime_sort")
    except ac.LockHeld:
        log.info("vorige run nog actief, exit")
        return 0

    log.info("=== anime_sort start (dry_run=%s, langs=%s, cap=%d) ===",
             args.dry_run, sorted(ac.ANIME_LANGS), MAX_MOVES)
    budget = [MAX_MOVES]
    mv_names: list[str] = []
    sr_names: list[str] = []
    conflicts: list[str] = []
    try:
        # ---- Radarr (films) ----
        r_root = ac.resolve_rootfolder("radarr", "/anime-movies")
        r_qp, r_qp_name = ac.resolve_quality_profile("radarr", "anime")
        if not r_root or not r_qp:
            log.error("radarr: anime-root (%s) of -profiel (%s) niet gevonden, films overgeslagen",
                      r_root, r_qp)
        else:
            log.info("radarr: root=%s profiel=%s(%s)", r_root, r_qp_name, r_qp)
            movies = ac.arr_get("radarr", "/movie") or []
            cands = [m for m in movies if not _in_anime_root(m) and is_anime_movie(m)]
            log.info("radarr: %d film-kandidaat(en) van %d", len(cands), len(movies))
            n, names, conf = move_movies(cands, r_root, ac.ANIME_MOVIES_HOST, r_qp,
                                         args.dry_run, log, budget)
            mv_names += names
            conflicts += conf

        # ---- Sonarr (series) ----
        s_root = ac.resolve_rootfolder("sonarr", "/anime-tv")
        s_qp, s_qp_name = ac.resolve_quality_profile("sonarr", "anime")
        if not s_root or not s_qp:
            log.error("sonarr: anime-root (%s) of -profiel (%s) niet gevonden, series overgeslagen",
                      s_root, s_qp)
        else:
            log.info("sonarr: root=%s profiel=%s(%s)", s_root, s_qp_name, s_qp)
            series = ac.arr_get("sonarr", "/series") or []
            cands = [s for s in series if not _in_anime_root(s) and is_anime_series(s)]
            log.info("sonarr: %d serie-kandidaat(en) van %d", len(cands), len(series))
            n, names, conf = move_series(cands, s_root, ac.ANIME_TV_HOST, s_qp,
                                         args.dry_run, log, budget)
            sr_names += names
            conflicts += conf

        log.info("=== klaar: %d film(s) + %d serie(s) %s, %d conflict(en) ===",
                 len(mv_names), len(sr_names),
                 "(dry-run)" if args.dry_run else "verplaatst", len(conflicts))

        if not args.dry_run and (mv_names or sr_names) and ac.EMBY_API_KEY:
            ac.emby_refresh_library(log)

        _telegram(args.dry_run, mv_names, sr_names, conflicts, log)
        return 0
    except Exception:  # noqa: BLE001
        log.exception("onverwachte fout")
        ac.send_telegram(f"<b>{ac.HOST_LABEL}</b>\n<b>anime-sort — FOUT</b>\nZie log.", log)
        return 1
    finally:
        ac.release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
