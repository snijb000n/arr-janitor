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
| `anime` | Scant alle Sonarr-series, detecteert welke anime zijn en zet ze op `seriesType=anime`, de anime-root (`/anime-tv`) en het profiel `Ultra-HD - Anime`. Verplaatst de files mee (`moveFiles`). Idempotent: al-correcte series worden overgeslagen. Eigen lock + eigen cron-regel (05:00), **niet** onderdeel van `all`. |
| `plexlang` | Zet in Plex per aflevering de default **audiotrack** op de oorspronkelijke taal (meestal Japans, soms Koreaans/Chinees), **alleen voor anime**. Een Plex-show wordt enkel aangeraakt als hij op tvdbId/tmdbId matcht met een Sonarr-serie met `seriesType=anime`. Originele taal komt uit TMDb (`original_language`), met fallback `PLEX_AUDIO_FALLBACK`. Ondertitels blijven onaangeroerd. Idempotent, gecapt, eigen lock + eigen cron (05:30), **niet** onderdeel van `all`. |

## Flags

- `--dry-run` — log alle voorgenomen acties, voer geen API-mutatie of file-move uit.
- `--verbose` / `-v` — DEBUG-niveau logging.

## Eerste gebruik

```bash
cd /pad/naar/arr-janitor   # bijv. ~/scripts/arr-janitor
# 1. Machine-setup: genereert machine.env + config.env, print cron-regels
./setup.sh
# 2. Vul de API-keys in config.env in, daarna:

# 3. Lees-test (geen mutaties)
python3 arr_janitor.py all --dry-run --verbose

# 4. Subcommando's los testen, eerst dry-run, dan echt
python3 arr_janitor.py extract --dry-run
python3 arr_janitor.py import --dry-run --verbose
python3 arr_janitor.py clean  --dry-run --verbose
```

## Cron (5x per nacht)

Als de user die de media beheert (op deze server: root). `./setup.sh` print
deze regels met de juiste paden ingevuld.

```cron
0 22,0,2,4,6 * * * /usr/bin/python3 /pad/naar/arr-janitor/arr_janitor.py all >> /pad/naar/arr-janitor/cron.log 2>&1
```

Runs: 22:00, 00:00, 02:00, 04:00, 06:00.

Plus de anime-reclassify, dagelijks 05:00 (eigen lock, los van `all`):

```cron
0 5 * * * /usr/bin/python3 /pad/naar/arr-janitor/arr_janitor.py anime >> /pad/naar/arr-janitor/cron.log 2>&1
```

Plus de plex audio-language, dagelijks 05:30 (na de anime-reclassify, eigen lock):

```cron
30 5 * * * /usr/bin/python3 /pad/naar/arr-janitor/arr_janitor.py plexlang >> /pad/naar/arr-janitor/cron.log 2>&1
```

Plus dv-guard, dagelijks 05:00 (eigen lock, los script):

```cron
0 5 * * * /usr/bin/python3 /pad/naar/arr-janitor/dv_guard.py >> /pad/naar/arr-janitor/cron.log 2>&1
```

### dv-guard (`dv_guard.py`)

Los script (geen subcommando) dat de hele Radarr/Sonarr-bibliotheek scant op
**Dolby Vision**- en **3D**-bestanden en voor de gevonden items automatisch
vervangende searches triggert (`MoviesSearch`/`SeasonSearch`). Detectie is
dubbel: bestandsnaam-patronen (`DV`, `DoVi`, `Dolby Vision`, DV-hybrides, `3D`,
`SBS`, `OU/TAB`) én echte codec-metadata via `ffprobe` (DOVI configuration
record, 8 parallelle workers). Uitzonderingslijst voor titels met "3D" in de
naam (*Saw 3D* e.d.).

Vereist in Radarr én Sonarr de custom formats **'Dolby Vision (Block)'**,
**'3D (Block)'** en **'All Releases (Baseline)'** — die zorgen dat de
vervangende grab geen DV/3D-release pakt. Het script waarschuwt als ze
ontbreken. Vereist `ffprobe` op het systeem. Flags: `--dry-run` (geen
searches/deletes/grabs), `--verbose`. Machinepaden via `machine.env`
(zie hieronder).

**Levenscyclus & vervang-motor.** Radarr/Sonarr downgraden nooit uit
zichzelf, dus DV-bestanden waarvoor geen gelijkwaardige non-DV release
bestaat blijven anders eeuwig staan. Daarom (state in
`dv_guard_state.json`, gitignored):

1. Nieuw gedetecteerd item → `active`: doet `DV_ACTIVE_NIGHTS` (7) nachten
   mee in de nachtelijke batch-search (native upgrade-kans).
2. Daarna → `parked` (skip-lijst): geen nachtelijke search meer — scheelt
   honderden zinloze indexer-searches per nacht.
3. Op `DV_REPLACE_WEEKDAY` (zondag) draait de **vervang-motor** over de
   geparkeerde items, max `DV_REPLACE_CAP` (15) pogingen per run:
   interactieve indexer-search → beste non-DV release volgens de
   **kwaliteitsladder van het eigen quality profile** (profielvolgorde
   hoog→laag: eerst WEB-2160p, desnoods 1080p, dan 720p) → pas als er een
   kandidaat is wordt de oude file verwijderd en die release gegrabt.
   Mislukt de download alsnog, dan staat het item op 'missing' en zoekt
   Radarr/Sonarr/huntarr native verder (zonder bestaand bestand geldt de
   downgrade-blokkade niet meer). Niets gevonden → geparkeerd tot de
   volgende zondag.
4. Blijkt een item ná vervanging opnieuw DV (codec in de nieuwe file), dan
   wordt het permanent geparkeerd en door de motor overgeslagen
   (loop-preventie; handmatige aandacht nodig).

Bij series: per-aflevering grabs; een season pack alleen als het héle
seizoen DV is. Config-keys: zie `machine.env.example`.

**Direct delete (`DV_DELETE_IMMEDIATE=true`, opt-in per machine).** DV/3D
is hier per definitie onafspeelbaar, dus gedetecteerde files mogen meteen
weg zonder te wachten op een vervangings-kandidaat. Bij detectie wordt de
file direct verwijderd en het item meegenomen in de batch-search; het staat
dan 'missing', waardoor Radarr/Sonarr (en huntarr) native de beste
toegestane release grabben via de profiel-ladder — DV/3D blijft geblokt
door de custom formats. De loop-preventie blijft: een item dat na
vervanging opnieuw DV blijkt wordt níét nogmaals verwijderd maar geparkeerd
(markers verlopen na 90 dagen). Default `false`.

### anime-detectie

Een serie geldt als anime als één van deze waar is:
1. `seriesType` is al `anime`, **of**
2. Sonarr's genre-lijst (van TheTVDB) bevat `Anime`, **of**
3. TMDb zegt anime — keyword `anime` (210024) óf genre Animation + origin
   country `JP`. Vereist een gratis `TMDB_API_KEY` in `config.env`; leeg = stap 3
   wordt overgeslagen (alleen TVDB-genre). Zet `ANIME_DETECTION=both|tvdb|tmdb`.

False positives pin je in `ANIME_EXCLUDE_IDS` (komma-gescheiden Sonarr series-id's).
Cap per run: `MAX_ANIME_RECLASSIFY_PER_RUN` (default 25). Voor de eerste
migratie in één keer: cap tijdelijk verhogen en `anime --verbose` los draaien.

### plex audio language (`plexlang`)

Plex' audio-taalvoorkeur is **per account en globaal** — je kunt die niet per
bibliotheek instellen. Om anime tóch automatisch in de originele taal te spelen
zónder gewone TV/films te raken, zet `plexlang` de **default audiotrack per
aflevering** via de Plex API (`PUT /library/parts/{id}?audioStreamID=...`).
Omkeerbaar, wijzigt geen bestanden, geldt server-breed voor wie zelf geen track
koos.

Anime-only is *by construction*: een Plex-show wordt alleen aangeraakt als hij
op **tvdbId/tmdbId** matcht met een Sonarr-serie met `seriesType=anime` (precies
de series die het `anime`-commando classificeert — "in de anime-map of met de
anime-tag"). Geen ID-match → overgeslagen. Niet-anime wordt dus nooit geraakt.

De originele taal komt per serie uit **TMDb** (`original_language`, vereist
`TMDB_API_KEY`) en wordt op de bijbehorende audiotrack gezet (`ja→jpn`,
`ko→kor`, `zh→zho/chi/cmn/yue`). Geen TMDb-key of geen resultaat → de
`PLEX_AUDIO_FALLBACK`-volgorde (default `jpn,kor,zho`). Bij meerdere matchende
tracks wint de hoogste kanaaltelling. Al-geselecteerde tracks worden
overgeslagen (idempotent). **Ondertitels worden niet aangeraakt** — die regelt
het aparte subtitle-script.

Config: `PLEX_URL`, `PLEX_TOKEN`, optioneel `PLEX_ANIME_SECTIONS` (sectienamen),
`PLEX_AUDIO_FALLBACK`, en cap `MAX_PLEX_AUDIO_PARTS_PER_RUN` (default 1000 — de
eerste backfill kan groot zijn; daarna blijft het laag). Token ophalen: in de
Plex-webapp een item → `…` → Get Info → **View XML**; de URL bevat
`X-Plex-Token=...`.

## Prerequisites

| Tool | Status op deze server | Nodig voor |
|---|---|---|
| Python 3.11 + `requests` | ✓ aanwezig | alles |
| `7z` | ✓ aanwezig | extract `.zip` `.7z` |
| `unrar` of `unar` | **ontbreekt** — `sudo apt install unrar` | extract `.rar` |

Zonder `unrar` worden `.rar` archieven gewoon overgeslagen met een WARNING — de
rest blijft werken. nzbget pakt al uit (Unpack=yes voor beide categorieën),
dus dit komt zelden voor.

## Configuratie (`config.env`)

Mode 600. Bevat API-keys + paden + drempels. Zie het bestand voor uitleg.

Belangrijkste schakelaar:
- `IMPORT_MODE=Copy` — origineel blijft staan na import (veilig, rollback mogelijk).
- Na ~2 weken zonder problemen → `IMPORT_MODE=Move` zodat `completed/` wordt opgeruimd.

## Machine-configuratie (`machine.env`)

Naast `config.env` (arr-janitor API-keys + drempels) is er `machine.env`:
machinepaden voor de anime-scripts (`anime_sort.py`, `anime_audio.py`).
Gegenereerd door `./setup.sh`, gitignored. Keys: `SECRETS_FILE` (fleet-brede
secrets), `LOG_DIR`, `MEDIA_ROOT` (+ optionele per-map overrides, zie
`machine.env.example`) en `HOST_LABEL` (machinenaam in Telegram-berichten,
default de hostname). Geen `machine.env` → de defaults in `anime_common.py`
(= de waarden van TheBeastServer). Echte env-vars winnen altijd van beide
bestanden.

## Logging

- `arr_janitor.log` — rotating, 5 MB × 5. Eén regel per actie.
- `cron.log` — cron's stdout/stderr (zou leeg moeten blijven, alleen vangnet).

## Lockfile

`fcntl.flock` op `.lock`. Als een vorige run nog draait, exit het script
direct met code 0 en logt "previous run still active". Voorkomt overlap
zonder cron-gymnastiek.

## Veiligheid

- Geen sudo, geen recursive deletes buiten geconfigureerde roots.
- Pad-validatie: alle paden worden `Path.resolve()` en moeten binnen één van
  de geconfigureerde roots vallen.
- Caps: max 50 imports en max 20 removals per run.
- API retry: 3x op 5xx, exponential backoff, 30s timeout.

## Troubleshooting

**"FATAL: missing config file"** — je draait niet vanuit de juiste cwd of
`config.env` bestaat niet. Het script zoekt `config.env` naast zichzelf.

**"manualimport GET failed: 401"** — verkeerde API-key. Pak hem opnieuw uit
de `config.xml` in de data-map van je Radarr/Sonarr-container (op deze server:
`/home/sven/docker/{radarr,sonarr}/data/config.xml`, `<ApiKey>`).

**Imports doen niets** — check `arr_janitor.log` op `WARNING import[xxx]: skip`.
Toont rejections. Vaakste oorzaken: film/serie nog niet aan Radarr/Sonarr
toegevoegd, of file-naam niet matchbaar. Voeg de film/serie eerst toe of hernoem
de file zodat de parser het oppikt.

**Cron draait niet** — `crontab -l` checken (of `sudo crontab -l` als de cron
onder root draait); `journalctl -u cron --since "1 hour ago"`.

## Wat dit script niet doet

- Geen "search missing" — gebruik huntarr (`http://localhost:9705`).
- Geen webhooks/realtime triggers — bewust cron, simpeler.
- Geen wijzigingen aan nzbget/Docker config.
- Geen notifications — kan later via huntarr's eigen webhook.
