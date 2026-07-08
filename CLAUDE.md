# arr-janitor — regels voor wijzigingen

Deze repo draait op **meerdere machines** (TheBeastServer van sven + de Debian-
server van zijn kennis). Elke wijziging of toevoeging moet daarom
**machine-portable** zijn.

## Portabiliteitsregel (verplicht bij elke toevoeging)

- **Geen hardcoded machine-specifieke waarden in code**: geen `/home/<user>`-paden,
  usernames of hostnames. Controleer nieuwe/gewijzigde bestanden met:
  `grep -rn "/home/\|laominecon\|TheBeastServer" <file>`
- Machine-specifieke waarden gaan via **`machine.env`** (gegenereerd door
  `./setup.sh`, gitignored). De waarden van TheBeastServer staan als *defaults*
  in `anime_common.py` — nieuwe machine-keys daar toevoegen mét default, en in
  `machine.env.example` + `setup.sh` documenteren.
- `arr_janitor.py`-instellingen (API-keys, drempels) gaan via **`config.env`**
  (template: `config.env.example`), paden daarin relatief aan
  `SCRIPT_DIR = Path(__file__).resolve().parent`.

## Hergebruik `anime_common.py`

Nieuwe Python-scripts importeren `anime_common as ac` en gebruiken:
- `ac.SECRETS_PATH` / `ac.LOG_DIR` / `ac.MEDIA_ROOT` / `ac.HOST_LABEL`
- `ac.RADARR_ROOT_HOST` / `ac.SONARR_ROOT_HOST` (container→host padmapping)
- `ac.setup_logging(name, verbose)`, `ac.acquire_lock(name)` / `ac.LockHeld`
- `ac.arr_get/arr_post/arr_put`, `ac.emby_get/emby_post`, `ac.send_telegram`
- `ac.require_keys()`

Conventies: argparse met `--dry-run` en `--verbose`, eigen lock per script,
Nederlandstalige log/docs.

## dotenv-volgorde (niet wijzigen)

`anime_common.py` laadt eerst `machine.env`, daarna pas `SECRETS_FILE`.
`load_dotenv` overschrijft bestaande env-vars niet, dus:
**echte env-vars (cron) > machine.env > secrets-file**. Nooit `override=True`.

## Verificatie vóór commit

- `python3 -m py_compile <files>` en `bash -n <shell-scripts>`
- Dummy-secrets import-test (de echte secrets-file is op TheBeastServer
  root-owned; als gewone user niet leesbaar):
  `SECRETS_FILE=/tmp/dummy.env python3 -c "import <module>"` — met en zonder
  `MEDIA_ROOT`-override, en check dat de defaults byte-identiek blijven.
- Let op: de crons draaien op TheBeastServer als **root**; gedrag zonder
  `machine.env` moet identiek blijven aan de defaults.

## Machines

| Machine | User | Status |
|---|---|---|
| TheBeastServer | sven (crons als root) | actief; `machine.env` gegenereerd |
| Debian-server kennis | n.t.b. | pending: `git pull` + `./setup.sh` |
