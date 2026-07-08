#!/usr/bin/env python3
"""DV Guard - Controleert op Dolby Vision en 3D bestanden in Radarr/Sonarr
en triggert automatisch vervangende downloads.

Controleert zowel bestandsnamen ALS de daadwerkelijke codec-metadata via
ffprobe. Vereist de custom formats 'Dolby Vision (Block)', '3D (Block)' en
'All Releases (Baseline)' in Radarr/Sonarr (het script waarschuwt als ze
ontbreken).

Config via anime_common: machine.env (SECRETS_FILE/LOG_DIR/MEDIA_ROOT) +
fleet-secrets. Draait elke nacht om 05:00 via cron, eigen lock.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import anime_common as ac

FFPROBE_WORKERS = 8

# Bestandsnaam-patronen
DV_PATTERN = re.compile(r'\b(DV|DoVi|Dolby[. ]?Vision)\b', re.IGNORECASE)
DV_HYBRID_PATTERN = re.compile(r'\bDV[. ]?(HDR10(Plus|\+)?|HLG|SDR)\b', re.IGNORECASE)
THREE_D_PATTERN = re.compile(r'\b3D\b', re.IGNORECASE)
SBS_PATTERN = re.compile(r'\b(H?(alf[. ]?)?SBS)\b', re.IGNORECASE)
OU_PATTERN = re.compile(r'\b(H?(alf[. ]?)?OU|TAB)\b', re.IGNORECASE)

FALSE_3D_TITLES = {'Saw 3D', 'Step Up 3D', 'Piranha 3D', 'Friday the 13th Part 3D'}

# Radarr/Sonarr pad-mapping naar host-pad, afgeleid van MEDIA_ROOT (machine.env).
# Bewust zonder de anime-roots: anime-bestanden krijgen wel de naamcheck maar
# geen ffprobe-check (zelfde gedrag als het oorspronkelijke dv-guard script).
PATH_MAPPINGS = {
    f"{container}/": f"{host}/"
    for container, host in {**ac.RADARR_ROOT_HOST, **ac.SONARR_ROOT_HOST}.items()
    if "anime" not in container
}

log = None  # gezet in main()


def container_path_to_host(container_path):
    """Vertaal een container-pad naar een host-pad."""
    for container_prefix, host_prefix in PATH_MAPPINGS.items():
        if container_path.startswith(container_prefix):
            return host_prefix + container_path[len(container_prefix):]
    return container_path


def is_blocked_by_name(filename):
    """Check of een bestandsnaam DV of 3D formaat bevat."""
    if DV_PATTERN.search(filename) or DV_HYBRID_PATTERN.search(filename):
        return 'DV (naam)'
    if THREE_D_PATTERN.search(filename) or SBS_PATTERN.search(filename) or OU_PATTERN.search(filename):
        return '3D (naam)'
    return None


def ffprobe_check_dv(filepath):
    """Check via ffprobe of een bestand Dolby Vision metadata bevat."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', filepath],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for stream in data.get('streams', []):
            if stream.get('codec_type') != 'video':
                continue
            for side_data in (stream.get('side_data_list') or []):
                if side_data.get('side_data_type') == 'DOVI configuration record':
                    return 'DV (codec)'
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def check_radarr(dry_run):
    """Vind DV/3D films in Radarr en trigger vervangende searches."""
    log.info('Radarr: bestanden controleren...')
    movies = ac.arr_get('radarr', '/movie')

    blocked_ids = []
    need_ffprobe = []  # Films die de naamcheck doorstaan maar nog ffprobe nodig hebben

    for movie in movies:
        movie_file = movie.get('movieFile')
        if not movie_file:
            continue
        path = movie_file.get('relativePath', '')
        full_path = movie_file.get('path', '')

        # Stap 1: snelle naamcheck
        reason = is_blocked_by_name(path)
        if reason:
            if reason == '3D (naam)' and movie.get('title', '') in FALSE_3D_TITLES:
                continue
            blocked_ids.append(movie['id'])
            log.info(f'  [{reason}] {movie["title"]} ({movie.get("year", "?")}) - {path}')
        else:
            # Stap 2: ffprobe nodig voor bestanden zonder DV/3D in de naam
            host_path = container_path_to_host(full_path)
            need_ffprobe.append((movie, host_path))

    # Parallelle ffprobe checks voor bestanden zonder DV in de naam
    if need_ffprobe:
        log.info(f'Radarr: {len(need_ffprobe)} bestanden controleren via ffprobe...')
        with ThreadPoolExecutor(max_workers=FFPROBE_WORKERS) as executor:
            futures = {
                executor.submit(ffprobe_check_dv, host_path): movie
                for movie, host_path in need_ffprobe
            }
            for future in as_completed(futures):
                movie = futures[future]
                reason = future.result()
                if reason:
                    blocked_ids.append(movie['id'])
                    path = movie['movieFile'].get('relativePath', '')
                    log.info(f'  [{reason}] {movie["title"]} ({movie.get("year", "?")}) - {path}')

    if not blocked_ids:
        log.info('Radarr: geen DV/3D bestanden gevonden.')
        return 0

    if dry_run:
        log.info(f'[dry-run] Radarr: MoviesSearch voor {len(blocked_ids)} films overgeslagen')
        return len(blocked_ids)

    log.info(f'Radarr: {len(blocked_ids)} DV/3D films gevonden, search starten...')
    result = ac.arr_post('radarr', '/command', {
        'name': 'MoviesSearch',
        'movieIds': blocked_ids
    })
    log.info(f'Radarr: MoviesSearch gestart (command id: {result.get("id")})')
    return len(blocked_ids)


def check_sonarr(dry_run):
    """Vind DV/3D afleveringen in Sonarr en trigger vervangende searches per seizoen."""
    log.info('Sonarr: bestanden controleren...')
    series_list = ac.arr_get('sonarr', '/series')

    affected_seasons = set()
    episode_count = 0
    need_ffprobe = []

    for series in series_list:
        try:
            files = ac.arr_get('sonarr', '/episodefile', seriesId=series['id'])
        except Exception:
            continue

        for ef in files:
            path = ef.get('relativePath', '')
            full_path = ef.get('path', '')

            reason = is_blocked_by_name(path)
            if reason:
                season = ef.get('seasonNumber', 0)
                affected_seasons.add((series['id'], season, series['title']))
                episode_count += 1
                log.info(f'  [{reason}] {series["title"]} S{season:02d} - {path}')
            else:
                host_path = container_path_to_host(full_path)
                need_ffprobe.append((series, ef, host_path))

    # Parallelle ffprobe checks
    if need_ffprobe:
        log.info(f'Sonarr: {len(need_ffprobe)} bestanden controleren via ffprobe...')
        with ThreadPoolExecutor(max_workers=FFPROBE_WORKERS) as executor:
            futures = {
                executor.submit(ffprobe_check_dv, host_path): (series, ef)
                for series, ef, host_path in need_ffprobe
            }
            for future in as_completed(futures):
                series, ef = futures[future]
                reason = future.result()
                if reason:
                    season = ef.get('seasonNumber', 0)
                    path = ef.get('relativePath', '')
                    affected_seasons.add((series['id'], season, series['title']))
                    episode_count += 1
                    log.info(f'  [{reason}] {series["title"]} S{season:02d} - {path}')

    if not affected_seasons:
        log.info('Sonarr: geen DV/3D bestanden gevonden.')
        return 0

    if dry_run:
        log.info(f'[dry-run] Sonarr: SeasonSearch voor {len(affected_seasons)} seizoenen '
                 f'({episode_count} afleveringen) overgeslagen')
        return episode_count

    log.info(f'Sonarr: {episode_count} DV/3D afleveringen in {len(affected_seasons)} seizoenen, searches starten...')

    for series_id, season_number, title in sorted(affected_seasons):
        try:
            result = ac.arr_post('sonarr', '/command', {
                'name': 'SeasonSearch',
                'seriesId': series_id,
                'seasonNumber': season_number
            })
            log.info(f'  SeasonSearch gestart: {title} S{season_number:02d} (command id: {result.get("id")})')
        except Exception as e:
            log.error(f'  SeasonSearch mislukt: {title} S{season_number:02d} - {e}')

    return episode_count


def verify_custom_formats():
    """Controleer of de DV/3D custom formats nog bestaan."""
    for name, which in [('Radarr', 'radarr'), ('Sonarr', 'sonarr')]:
        formats = ac.arr_get(which, '/customformat')
        format_names = [f['name'] for f in formats]

        missing = []
        for required in ['Dolby Vision (Block)', '3D (Block)', 'All Releases (Baseline)']:
            if required not in format_names:
                missing.append(required)

        if missing:
            log.warning(f'{name}: custom formats ontbreken: {", ".join(missing)}')
            log.warning(f'{name}: voer het setup-script opnieuw uit om deze aan te maken!')
        else:
            log.info(f'{name}: alle custom formats aanwezig.')


def main():
    global log
    parser = argparse.ArgumentParser(description='dv-guard')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    log = ac.setup_logging('dv_guard', args.verbose)

    missing = ac.require_keys()
    if missing:
        log.error(f'API keys ontbreken in de secrets-file: {", ".join(missing)}')
        return 1

    try:
        lock = ac.acquire_lock('dv_guard')
    except ac.LockHeld:
        log.info('vorige run nog actief, stop')
        return 0

    try:
        log.info('=' * 60)
        log.info(f'DV Guard gestart - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
                 + (' (DRY-RUN)' if args.dry_run else ''))
        log.info('=' * 60)

        try:
            verify_custom_formats()
        except Exception as e:
            log.error(f'Fout bij controleren custom formats: {e}')

        try:
            movie_count = check_radarr(args.dry_run)
        except Exception as e:
            log.error(f'Fout bij Radarr controle: {e}')
            movie_count = 0

        try:
            episode_count = check_sonarr(args.dry_run)
        except Exception as e:
            log.error(f'Fout bij Sonarr controle: {e}')
            episode_count = 0

        log.info('=' * 60)
        log.info(f'Klaar - {movie_count} films en {episode_count} afleveringen gevonden voor vervanging')
        log.info('=' * 60)
        return 0
    finally:
        ac.release_lock(lock)


if __name__ == '__main__':
    sys.exit(main())
