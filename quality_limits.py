#!/usr/bin/env python3
"""Quality Limits - Zet de Sonarr quality definitions (size-limieten) strak.

Sonarr keurt releases af die groter zijn dan maxSize (MB per minuut runtime)
uit de quality definitions. De defaults staan ruim: een lange aflevering
(60-80 min) in 2160p mocht tot ~9-11 GB. Dit script zet de limieten op:

  kwaliteitsgroep                       preferred  max    ~ per 45 min
  HDTV/WEBRip/WEBDL/Bluray-720p             25      30    max ~1,3 GB
  HDTV/WEBRip/WEBDL/Bluray-1080p            40      45    max ~2,0 GB
  HDTV/WEBRip/WEBDL/Bluray-2160p            60      68    max ~3,0 GB

Remux- en SD-kwaliteiten blijven bewust ongemoeid (remux zit in geen enkel
profiel, SD is al klein); minSize blijft zoals hij staat. Idempotent: alleen
afwijkende definities worden gePUT. Alleen Sonarr; Radarr is een bewuste
keuze om niet te limiteren (films mogen groot blijven).

Eenmalig draaien per machine (na git pull ook op de andere server).
Let op: de vervang-motor van dv_guard leest deze limieten via de API en
dwingt ze ook af bij zijn handmatige grabs.
"""
from __future__ import annotations

import argparse
import sys

import anime_common as ac

# kwaliteit-naam -> (preferredSize, maxSize) in MB per minuut
LIMITS = {
    'HDTV-720p': (25, 30), 'WEBRip-720p': (25, 30),
    'WEBDL-720p': (25, 30), 'Bluray-720p': (25, 30),
    'HDTV-1080p': (40, 45), 'WEBRip-1080p': (40, 45),
    'WEBDL-1080p': (40, 45), 'Bluray-1080p': (40, 45),
    'HDTV-2160p': (60, 68), 'WEBRip-2160p': (60, 68),
    'WEBDL-2160p': (60, 68), 'Bluray-2160p': (60, 68),
}

log = None  # gezet in main()


def apply_limits(dry_run: bool) -> int:
    """Zet afwijkende Sonarr quality definitions op de LIMITS-waarden."""
    changed = 0
    for qd in ac.arr_get('sonarr', '/qualitydefinition'):
        name = qd['quality']['name']
        if name not in LIMITS:
            log.debug(f'{name}: geen limiet gedefinieerd, ongemoeid')
            continue
        pref, mx = LIMITS[name]
        if qd.get('preferredSize') == pref and qd.get('maxSize') == mx:
            log.debug(f'{name}: staat al goed (pref={pref}, max={mx})')
            continue
        log.info(f'{name}: preferred {qd.get("preferredSize")} -> {pref}, '
                 f'max {qd.get("maxSize")} -> {mx}'
                 + (' [dry-run]' if dry_run else ''))
        if not dry_run:
            qd['preferredSize'] = pref
            qd['maxSize'] = mx
            ac.arr_put('sonarr', f'/qualitydefinition/{qd["id"]}', qd)
        changed += 1
    return changed


def main():
    global log
    parser = argparse.ArgumentParser(description='quality-limits')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    log = ac.setup_logging('quality_limits', args.verbose)

    missing = ac.require_keys()
    if missing:
        log.error(f'API keys ontbreken in de secrets-file: {", ".join(missing)}')
        return 1

    try:
        lock = ac.acquire_lock('quality_limits')
    except ac.LockHeld:
        log.info('vorige run nog actief, stop')
        return 0

    try:
        log.info(f'Quality Limits gestart op {ac.HOST_LABEL}'
                 + (' (DRY-RUN)' if args.dry_run else ''))
        changed = apply_limits(args.dry_run)
        if changed:
            log.info(f'Klaar - {changed} definitie(s) '
                     + ('zouden wijzigen' if args.dry_run else 'gewijzigd'))
        else:
            log.info('Klaar - alle definities stonden al goed')
        return 0
    finally:
        ac.release_lock(lock)


if __name__ == '__main__':
    sys.exit(main())
