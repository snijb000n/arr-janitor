# arr-janitor

Nachtelijke automatisering voor Radarr + Sonarr op deze server. Vervangt
handmatige imports, ruimt vastgelopen queue-items op, en pakt achtergebleven
archieven uit. Eén Python-script, geen extra dependencies behalve `requests`.

> Dit script doet **NIET** "automatisch zoeken naar ontbrekende afleveringen".
> Dat doet de bestaande `huntarr` container op `http://localhost:9705`. Configureer
> die afzonderlijk.

## Subcommando's

| Cmd | Doet |
|---|---|
| `extract` | Loopt `completed/Movies` en `completed/tv` door en pakt achtergebleven `.rar`/`.zip`/`.7z` uit. Idempotent (`.extracted` marker). Safety net — nzbget pakt zelf al uit. |
| `import` | Vraagt Radarr/Sonarr `manualimport` API om kandidaten in `completed/`. Pusht alleen kandidaten met **zekere match** (geen rejections, movie/series id aanwezig, voor Sonarr ook episodeIds non-empty). Twijfelgevallen worden gelogd. |
| `clean` | Verwijdert items uit de queue die `>= STALLED_HOURS` vastzitten of failed/warning-status hebben. Blocklist + optioneel re-search. |
| `all` | extract → import → clean. Gebruikt door cron. |

## Flags

- `--dry-run` — log alle voorgenomen acties, voer geen API-mutatie of file-move uit.
- `--verbose` / `-v` — DEBUG-niveau logging.

## Eerste gebruik

```bash
cd ~/scripts/arr-janitor
# 1. Lees-test (geen mutaties)
python3 arr_janitor.py all --dry-run --verbose

# 2. Subcommando's los testen, eerst dry-run, dan echt
python3 arr_janitor.py extract --dry-run
python3 arr_janitor.py import --dry-run --verbose
python3 arr_janitor.py clean  --dry-run --verbose
```

## Cron (5x per nacht)

```cron
0 22,0,2,4,6 * * * /usr/bin/python3 /home/sven/scripts/arr-janitor/arr_janitor.py all >> /home/sven/scripts/arr-janitor/cron.log 2>&1
```

Runs: 22:00, 00:00, 02:00, 04:00, 06:00.

## Prerequisites

| Tool | Nodig voor |
|---|---|
| Python 3.11 + `requests` | alles |
| `7z` (`p7zip-full`) | extract `.zip` `.7z` |
| `unrar` of `unar` | extract `.rar` |

## Veiligheid

- Geen sudo, geen recursive deletes buiten geconfigureerde roots.
- Pad-validatie: alle paden worden `Path.resolve()` en moeten binnen één van
  de geconfigureerde roots vallen.
- Caps: max 50 imports en max 20 removals per run.
- API retry: 3x op 5xx, exponential backoff, 30s timeout.

## Lockfile

`fcntl.flock` op `.lock`. Als een vorige run nog draait, exit het script
direct met code 0 en logt "previous run still active". Voorkomt overlap
zonder cron-gymnastiek.

## Anime-scripts (los van het hoofdscript)

Twee aanvullende nachtelijke scripts, onafhankelijk van `arr_janitor.py`. Ze laden
config uit de gedeelde `/home/sven/scripts/secrets/config.env` (Radarr/Sonarr/Emby +
Telegram), delen helpers via `anime_common.py`, en hebben elk een eigen lockfile en log
in `/home/sven/scripts/logs/`.

| Script | Doet |
|---|---|
| `anime_sort.py` | Detecteert anime (genre Animation/Anime **én** originalLanguage Japans/Chinees/Koreaans via TVDB) die nog in `/movies` of `/tv` staat, en verplaatst die via de Radarr/Sonarr editor-API (`moveFiles=true`) naar `/anime-movies` / `/anime-tv`. Zet het Anime-quality-profiel; voor Japanse/al-anime series ook `seriesType=anime` (Chinese/Koreaanse `standard`-donghua blijven standard). Live-action Japans (geen Animation-genre) blijft staan. Bestaat de doelmap al mét bestanden (dubbele kopie) → overslaan + waarschuwen. |
| `anime_audio.py` | Zet per anime-bestand de audiotrack in de **originele taal** (per titel uit TVDB) als default-track-flag — `mkvpropedit` voor `.mkv`, `ffmpeg -c copy` remux voor `.mp4` — zodat Emby/Plex standaard de originele audio (jpn/zho/kor/…) speelt. Idempotent (slaat bestanden die al goed staan over), met state-cache op pad+mtime. Triggert daarna een Emby library-refresh. |

Flags: `--dry-run`, `--verbose` (en `anime_audio.py` ook `--no-cache`).

Optionele config-keys (defaults in code): `ANIME_LANGS` (default `Japanese,Chinese,Korean`),
`ANIME_MAX_MOVES_PER_RUN` (20), `ANIME_AUDIO_MAX_PER_RUN` (1000).

Extra prerequisites: `mkvtoolnix` (`mkvpropedit` + `mkvmerge`) naast `ffmpeg`/`ffprobe`.

Cron (na arr-janitor om 22/00/02/04/06 en dv-guard om 05:00; sort vóór audio zodat
nieuw-verplaatste anime dezelfde nacht audio-gefixt wordt):

```cron
0 3 * * *  /usr/bin/python3 /home/sven/scripts/arr-janitor/anime_sort.py  >> /home/sven/scripts/logs/anime_sort_cron.log 2>&1
30 3 * * * /usr/bin/python3 /home/sven/scripts/arr-janitor/anime_audio.py >> /home/sven/scripts/logs/anime_audio_cron.log 2>&1
```
