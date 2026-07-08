#!/usr/bin/env python3
"""DV Guard - Controleert op Dolby Vision en 3D bestanden in Radarr/Sonarr
en vervangt ze door non-DV releases.

Detectie is dubbel: bestandsnaam-patronen ALS de daadwerkelijke codec-metadata
via ffprobe (DOVI configuration record). Vereist de custom formats
'Dolby Vision (Block)', '3D (Block)' en 'All Releases (Baseline)' in
Radarr/Sonarr (het script waarschuwt als ze ontbreken).

Levenscyclus per gedetecteerd item (state in dv_guard_state.json):
  1. Nieuw item -> status 'active': DV_ACTIVE_NIGHTS nachten mee in de
     nachtelijke batch-search (native upgrade-kans).
  2. Daarna -> status 'parked' (skip-lijst): geen nachtelijke search meer.
  3. Op DV_REPLACE_WEEKDAY (default zondag) draait de vervang-motor over de
     geparkeerde items (max DV_REPLACE_CAP per run): interactieve
     indexer-search, beste non-DV release volgens de kwaliteitsladder van het
     eigen quality profile (hoog->laag, dus desnoods 1080p/720p), dan pas
     oude file verwijderen + release grabben. Niets gevonden -> geparkeerd
     tot de volgende motor-dag.
  4. Items die na een vervanging opnieuw DV blijken -> direct geparkeerd en
     door de motor overgeslagen (loop-preventie, handmatige aandacht nodig).

Config via anime_common: machine.env (SECRETS_FILE/LOG_DIR/MEDIA_ROOT) +
fleet-secrets. Motor-gedrag via env/machine.env: DV_ACTIVE_NIGHTS,
DV_REPLACE_WEEKDAY, DV_REPLACE_CAP, DV_REPLACE_ENABLE.
Draait elke nacht om 05:00 via cron, eigen lock.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anime_common as ac

FFPROBE_WORKERS = 8

STATE_PATH = Path(os.getenv("DV_STATE_FILE",
                            str(Path(__file__).resolve().parent / "dv_guard_state.json")))
DV_ACTIVE_NIGHTS = int(os.getenv("DV_ACTIVE_NIGHTS", "7"))
DV_REPLACE_WEEKDAY = int(os.getenv("DV_REPLACE_WEEKDAY", "6"))  # Python: 0=ma, 6=zo
DV_REPLACE_CAP = int(os.getenv("DV_REPLACE_CAP", "15"))
DV_REPLACE_ENABLE = os.getenv("DV_REPLACE_ENABLE", "true").strip().lower() in ("1", "true", "yes")

# Bestandsnaam-patronen
DV_PATTERN = re.compile(r'\b(DV|DoVi|Dolby[. ]?Vision)\b', re.IGNORECASE)
DV_HYBRID_PATTERN = re.compile(r'\bDV[. ]?(HDR10(Plus|\+)?|HLG|SDR)\b', re.IGNORECASE)
THREE_D_PATTERN = re.compile(r'\b3D\b', re.IGNORECASE)
SBS_PATTERN = re.compile(r'\b(H?(alf[. ]?)?SBS)\b', re.IGNORECASE)
OU_PATTERN = re.compile(r'\b(H?(alf[. ]?)?OU|TAB)\b', re.IGNORECASE)

FALSE_3D_TITLES = {'Saw 3D', 'Step Up 3D', 'Piranha 3D', 'Friday the 13th Part 3D'}

# Custom formats die een release voor ons diskwalificeren
BLOCK_CF_NAMES = {'Dolby Vision (Block)', '3D (Block)'}

# Radarr/Sonarr pad-mapping naar host-pad, afgeleid van MEDIA_ROOT (machine.env).
# Bewust zonder de anime-roots: anime-bestanden krijgen wel de naamcheck maar
# geen ffprobe-check (zelfde gedrag als het oorspronkelijke dv-guard script).
PATH_MAPPINGS = {
    f"{container}/": f"{host}/"
    for container, host in {**ac.RADARR_ROOT_HOST, **ac.SONARR_ROOT_HOST}.items()
    if "anime" not in container
}

log = None  # gezet in main()


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


# ---------- detectie ----------

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


def detect_radarr():
    """Vind DV/3D films in Radarr. Return {movie_id(str): movie}."""
    log.info('Radarr: bestanden controleren...')
    movies = ac.arr_get('radarr', '/movie')

    detected = {}
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
            detected[str(movie['id'])] = movie
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
                    detected[str(movie['id'])] = movie
                    path = movie['movieFile'].get('relativePath', '')
                    log.info(f'  [{reason}] {movie["title"]} ({movie.get("year", "?")}) - {path}')

    return detected


def detect_sonarr():
    """Vind DV/3D afleveringen in Sonarr.

    Return {"seriesId:season": {'series', 'season', 'dv_file_ids', 'total_files'}}.
    """
    log.info('Sonarr: bestanden controleren...')
    series_list = ac.arr_get('sonarr', '/series')

    seasons = {}
    totals = {}  # (series_id, season) -> totaal aantal episodefiles in dat seizoen
    need_ffprobe = []

    def _register(series, ef):
        season = ef.get('seasonNumber', 0)
        key = f"{series['id']}:{season}"
        entry = seasons.setdefault(key, {
            'series': series, 'season': season, 'dv_file_ids': [], 'total_files': 0})
        entry['dv_file_ids'].append(ef['id'])

    for series in series_list:
        try:
            files = ac.arr_get('sonarr', '/episodefile', seriesId=series['id'])
        except Exception:
            continue

        for ef in files:
            totals[(series['id'], ef.get('seasonNumber', 0))] = \
                totals.get((series['id'], ef.get('seasonNumber', 0)), 0) + 1
            path = ef.get('relativePath', '')
            full_path = ef.get('path', '')

            reason = is_blocked_by_name(path)
            if reason:
                _register(series, ef)
                log.info(f'  [{reason}] {series["title"]} S{ef.get("seasonNumber", 0):02d} - {path}')
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
                    _register(series, ef)
                    log.info(f'  [{reason}] {series["title"]} S{ef.get("seasonNumber", 0):02d} - '
                             f'{ef.get("relativePath", "")}')

    for key, entry in seasons.items():
        entry['total_files'] = totals.get((entry['series']['id'], entry['season']), 0)
    return seasons


# ---------- state ----------

def load_state():
    try:
        state = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault('movies', {})
    state.setdefault('seasons', {})
    state.setdefault('replaced', {}).setdefault('movies', {})
    state['replaced'].setdefault('seasons', {})
    return state


def save_state(state):
    tmp = STATE_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE_PATH)


def update_bucket(bucket, detected_keys, replaced, label):
    """Werk een state-bucket bij: nieuw=active, active telt nachten (na
    DV_ACTIVE_NIGHTS -> parked), eerder-vervangen -> direct parked,
    niet-meer-gedetecteerd -> weg."""
    parked_now = 0
    for key in detected_keys:
        entry = bucket.get(key)
        if entry is None:
            if key in replaced:
                bucket[key] = {'first_seen': _now(), 'nights': 0, 'status': 'parked'}
                log.warning(f'  {label} {key}: na eerdere vervanging opnieuw DV -> geparkeerd '
                            f'(handmatige aandacht nodig)')
            else:
                bucket[key] = {'first_seen': _now(), 'nights': 0, 'status': 'active'}
        elif entry.get('status') == 'active':
            entry['nights'] = entry.get('nights', 0) + 1
            if entry['nights'] >= DV_ACTIVE_NIGHTS:
                entry['status'] = 'parked'
                parked_now += 1
    gone = [k for k in bucket if k not in detected_keys]
    for k in gone:
        del bucket[k]
    if parked_now:
        log.info(f'{label}: {parked_now} item(s) geparkeerd na {DV_ACTIVE_NIGHTS} nachten zonder vervanging')
    if gone:
        log.info(f'{label}: {len(gone)} item(s) niet meer DV -> uit de lijst')


# ---------- nachtelijke batch-search (alleen 'active' items) ----------

def nightly_search(state, det_movies, det_seasons, dry_run):
    movie_ids = [int(k) for k, v in state['movies'].items()
                 if v['status'] == 'active' and det_movies.get(k)]
    if movie_ids:
        if dry_run:
            log.info(f'[dry-run] Radarr: MoviesSearch voor {len(movie_ids)} films overgeslagen')
        else:
            result = ac.arr_post('radarr', '/command', {'name': 'MoviesSearch', 'movieIds': movie_ids})
            log.info(f'Radarr: MoviesSearch voor {len(movie_ids)} active films (command id: {result.get("id")})')
    else:
        log.info('Radarr: geen active films voor nachtelijke search')

    season_keys = [k for k, v in state['seasons'].items()
                   if v['status'] == 'active' and det_seasons.get(k)]
    if not season_keys:
        log.info('Sonarr: geen active seizoenen voor nachtelijke search')
    elif dry_run:
        log.info(f'[dry-run] Sonarr: SeasonSearch voor {len(season_keys)} seizoenen overgeslagen')
    else:
        for key in sorted(season_keys):
            entry = det_seasons[key]
            try:
                result = ac.arr_post('sonarr', '/command', {
                    'name': 'SeasonSearch',
                    'seriesId': entry['series']['id'],
                    'seasonNumber': entry['season']})
                log.info(f'  SeasonSearch gestart: {entry["series"]["title"]} '
                         f'S{entry["season"]:02d} (command id: {result.get("id")})')
            except Exception as e:
                log.error(f'  SeasonSearch mislukt: {key} - {e}')


# ---------- vervang-motor ----------

def _release_search(which, **params):
    """Interactieve indexer-search; kan lang duren, dus eigen ruime timeout."""
    url, key = ac._arr_target(which)
    r = ac.SESSION.get(f"{url}/api/v3/release", headers={"X-Api-Key": key},
                       params=params, timeout=300)
    r.raise_for_status()
    return r.json() if r.content else []


def _profile_ladder(which, profile_id, cache):
    """Quality-id -> ladder-rang uit het profiel (hoger = betere kwaliteit).
    Alleen 'allowed' kwaliteiten; groepsleden delen een rang."""
    ck = (which, profile_id)
    if ck not in cache:
        profile = ac.arr_get(which, f'/qualityprofile/{profile_id}')
        ranks, rank = {}, 0
        for item in profile.get('items', []):
            if not item.get('allowed'):
                continue
            if item.get('items'):
                for sub in item['items']:
                    ranks[sub['quality']['id']] = rank
            else:
                ranks[item['quality']['id']] = rank
            rank += 1
        cache[ck] = ranks
    return cache[ck]


def _candidate_key(rel, ranks):
    """Sorteersleutel voor een acceptabele release, of None als afgekeurd.
    Volgorde: ladder-rang (profielkwaliteit), dan CF-score, dan seeders."""
    title = rel.get('title') or ''
    # Eigen naamcheck bovenop de CF's: vangt ook verkapte DV-releases die de
    # word-boundary regex van het custom format ontwijken (bv. "br.dvmp4").
    if is_blocked_by_name(title) or 'dvmp4' in title.lower():
        return None
    for cf in rel.get('customFormats') or []:
        if cf.get('name') in BLOCK_CF_NAMES:
            return None
    for rej in rel.get('rejections') or []:
        if 'blocklist' in rej.lower():
            return None
    qid = ((rel.get('quality') or {}).get('quality') or {}).get('id')
    if qid not in ranks:
        return None
    return (ranks[qid], rel.get('customFormatScore', 0), rel.get('seeders') or 0)


def _best_release(releases, ranks, full_season=None):
    best, best_key = None, None
    for rel in releases:
        if full_season is not None and bool(rel.get('fullSeason')) != full_season:
            continue
        key = _candidate_key(rel, ranks)
        if key is not None and (best_key is None or key > best_key):
            best, best_key = rel, key
    return best


def _grab(which, rel):
    ac.arr_post(which, '/release', {'guid': rel['guid'], 'indexerId': rel['indexerId']})


def _replace_movie(movie, ranks, dry_run):
    """Return True als (dry-run: zou) vervangen."""
    releases = _release_search('radarr', movieId=movie['id'])
    best = _best_release(releases, ranks)
    if best is None:
        log.info(f'  geen non-DV kandidaat voor {movie["title"]} ({len(releases)} releases bekeken)')
        return False
    qname = best['quality']['quality']['name']
    log.info(f'  kandidaat voor {movie["title"]}: {best.get("title")} '
             f'[{qname}, CF-score {best.get("customFormatScore")}, seeders {best.get("seeders")}]')
    if dry_run:
        log.info('  [dry-run] delete oude file + grab overgeslagen')
        return True
    ac.arr_delete('radarr', f'/moviefile/{movie["movieFile"]["id"]}')
    _grab('radarr', best)
    log.info(f'  {movie["title"]}: oude DV-file verwijderd, grab gestart')
    return True


def _replace_season(entry, ranks, dry_run):
    """Vervang de DV-afleveringen van een seizoen. Season pack alleen als het
    hele seizoen DV is; anders per aflevering. Return True als iets vervangen."""
    series, season = entry['series'], entry['season']
    title = f'{series["title"]} S{season:02d}'
    releases = _release_search('sonarr', seriesId=series['id'], seasonNumber=season)

    all_dv = entry['total_files'] and len(entry['dv_file_ids']) >= entry['total_files']
    if all_dv:
        pack = _best_release(releases, ranks, full_season=True)
        if pack is not None:
            qname = pack['quality']['quality']['name']
            log.info(f'  kandidaat (season pack) voor {title}: {pack.get("title")} '
                     f'[{qname}, CF-score {pack.get("customFormatScore")}]')
            if dry_run:
                log.info('  [dry-run] delete episodefiles + grab overgeslagen')
                return True
            for fid in entry['dv_file_ids']:
                ac.arr_delete('sonarr', f'/episodefile/{fid}')
            _grab('sonarr', pack)
            log.info(f'  {title}: {len(entry["dv_file_ids"])} DV-files verwijderd, season pack grab gestart')
            return True
        log.info(f'  {title}: geen non-DV season pack, probeer per aflevering...')

    episodes = ac.arr_get('sonarr', '/episode', seriesId=series['id'])
    file_to_ep = {e['episodeFileId']: e for e in episodes if e.get('episodeFileId')}
    replaced = 0
    for fid in entry['dv_file_ids']:
        ep = file_to_ep.get(fid)
        if ep is None:
            continue
        epnum = ep.get('episodeNumber')
        candidates = [r for r in releases
                      if not r.get('fullSeason')
                      and (r.get('episodeNumbers') or r.get('mappedEpisodeNumbers') or []) == [epnum]]
        best = _best_release(candidates, ranks)
        if best is None:
            log.info(f'  geen non-DV kandidaat voor {title}E{epnum:02d}')
            continue
        qname = best['quality']['quality']['name']
        log.info(f'  kandidaat voor {title}E{epnum:02d}: {best.get("title")} '
                 f'[{qname}, CF-score {best.get("customFormatScore")}]')
        if dry_run:
            log.info('  [dry-run] delete episodefile + grab overgeslagen')
            replaced += 1
            continue
        ac.arr_delete('sonarr', f'/episodefile/{fid}')
        _grab('sonarr', best)
        replaced += 1
    if replaced and not dry_run:
        log.info(f'  {title}: {replaced} afleveringen vervangen')
    return replaced > 0


def replace_engine(state, det_movies, det_seasons, dry_run):
    """Motor-dag: probeer geparkeerde items te vervangen, max DV_REPLACE_CAP
    pogingen per run (interactieve searches zijn duur voor de indexers)."""
    budget = DV_REPLACE_CAP
    ladder_cache = {}
    log.info(f'Vervang-motor gestart (cap {DV_REPLACE_CAP}, dry_run={dry_run})')

    for key, entry in sorted(state['movies'].items()):
        if budget <= 0:
            break
        if entry['status'] != 'parked' or not det_movies.get(key):
            continue
        if key in state['replaced']['movies']:
            continue  # al eens vervangen en toch weer DV -> handmatig
        movie = det_movies[key]
        budget -= 1
        entry['last_attempt'] = _now()
        try:
            ranks = _profile_ladder('radarr', movie['qualityProfileId'], ladder_cache)
            if _replace_movie(movie, ranks, dry_run) and not dry_run:
                state['replaced']['movies'][key] = _now()
        except Exception as e:
            log.error(f'  vervangen mislukt voor movie {key}: {e}')

    for key, entry in sorted(state['seasons'].items()):
        if budget <= 0:
            break
        if entry['status'] != 'parked' or not det_seasons.get(key):
            continue
        if key in state['replaced']['seasons']:
            continue
        det = det_seasons[key]
        budget -= 1
        entry['last_attempt'] = _now()
        try:
            ranks = _profile_ladder('sonarr', det['series']['qualityProfileId'], ladder_cache)
            if _replace_season(det, ranks, dry_run) and not dry_run:
                state['replaced']['seasons'][key] = _now()
        except Exception as e:
            log.error(f'  vervangen mislukt voor seizoen {key}: {e}')

    if budget <= 0:
        log.info('Vervang-motor: cap bereikt, rest volgt volgende motor-dag')
    log.info('Vervang-motor klaar')


# ---------- custom formats ----------

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


# ---------- main ----------

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

        state = load_state()

        try:
            det_movies = detect_radarr()
        except Exception as e:
            log.error(f'Fout bij Radarr controle: {e}')
            det_movies = {k: None for k in state['movies']}  # state niet slopen bij API-fout

        try:
            det_seasons = detect_sonarr()
        except Exception as e:
            log.error(f'Fout bij Sonarr controle: {e}')
            det_seasons = {k: None for k in state['seasons']}

        update_bucket(state['movies'], set(det_movies), state['replaced']['movies'], 'Radarr')
        update_bucket(state['seasons'], set(det_seasons), state['replaced']['seasons'], 'Sonarr')

        nightly_search(state, det_movies, det_seasons, args.dry_run)

        if not DV_REPLACE_ENABLE:
            log.info('Vervang-motor uitgeschakeld (DV_REPLACE_ENABLE)')
        elif datetime.now().weekday() != DV_REPLACE_WEEKDAY:
            parked = sum(1 for v in state['movies'].values() if v['status'] == 'parked') + \
                     sum(1 for v in state['seasons'].values() if v['status'] == 'parked')
            log.info(f'Geen motor-dag (weekday {datetime.now().weekday()} != {DV_REPLACE_WEEKDAY}); '
                     f'{parked} item(s) geparkeerd')
        else:
            replace_engine(state, det_movies, det_seasons, args.dry_run)

        if not args.dry_run:
            try:
                save_state(state)
            except Exception as e:
                log.error(f'state opslaan mislukt: {e}')

        episode_count = sum(len(v['dv_file_ids']) for v in det_seasons.values() if v)
        active_m = sum(1 for v in state['movies'].values() if v['status'] == 'active')
        active_s = sum(1 for v in state['seasons'].values() if v['status'] == 'active')
        log.info('=' * 60)
        log.info(f'Klaar - {len(det_movies)} films en {episode_count} afleveringen DV/3D '
                 f'(active: {active_m} films / {active_s} seizoenen, '
                 f'geparkeerd: {len(state["movies"]) - active_m} films / '
                 f'{len(state["seasons"]) - active_s} seizoenen)')
        log.info('=' * 60)
        return 0
    finally:
        ac.release_lock(lock)


if __name__ == '__main__':
    sys.exit(main())
