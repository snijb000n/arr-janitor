#!/usr/bin/env bash
# setup.sh — maakt deze arr-janitor-checkout klaar voor deze machine.
# Genereert machine.env (machinepaden voor de anime-scripts), maakt config.env
# aan uit het template als die ontbreekt, en print de cron-regels met de juiste
# paden. Idempotent, geen sudo nodig.
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"

echo "arr-janitor setup — machine: $(hostname), user: $(id -un), dir: $SCRIPT_DIR"
echo ""

# --- dependencies check (waarschuwing, geen harde fail) ---
command -v python3 >/dev/null || echo "WAARSCHUWING: python3 niet gevonden"
python3 -c "import requests" 2>/dev/null || echo "WAARSCHUWING: python3-requests ontbreekt"
python3 -c "import dotenv"   2>/dev/null || echo "WAARSCHUWING: python-dotenv ontbreekt (nodig voor anime-scripts)"
command -v ffprobe >/dev/null || echo "WAARSCHUWING: ffprobe ontbreekt (nodig voor dv_guard.py)"

# --- helper: prompt met default ---
ask() {  # ask VAR "vraag" "default"
  local var="$1" prompt="$2" def="$3" val
  read -r -p "$prompt [$def]: " val
  printf -v "$var" '%s' "${val:-$def}"
}

# --- bestaande machine.env als default-bron ---
MACHINE_ENV="$SCRIPT_DIR/machine.env"
# shellcheck disable=SC1090
[ -f "$MACHINE_ENV" ] && . "$MACHINE_ENV"   # vult defaults met huidige waarden

ask SECRETS_FILE "Pad naar secrets-file"                        "${SECRETS_FILE:-$HOME/scripts/secrets/config.env}"
ask LOG_DIR      "Logdirectory"                                 "${LOG_DIR:-$HOME/scripts/logs}"
ask MEDIA_ROOT   "Media-root (met movies/, tv/, Anime-tv/ ...)" "${MEDIA_ROOT:-$HOME/media}"
ask HOST_LABEL   "Machinenaam voor Telegram"                    "${HOST_LABEL:-$(hostname)}"

# --- machine.env schrijven ---
cat > "$MACHINE_ENV" <<EOF
# Gegenereerd door setup.sh op $(date -Iseconds) — hostname $(hostname)
SECRETS_FILE=$SECRETS_FILE
LOG_DIR=$LOG_DIR
MEDIA_ROOT=$MEDIA_ROOT
HOST_LABEL=$HOST_LABEL
EOF
echo ""
echo "machine.env geschreven: $MACHINE_ENV"

# --- config.env uit template (alleen als afwezig) ---
if [ ! -f "$SCRIPT_DIR/config.env" ]; then
  cp "$SCRIPT_DIR/config.env.example" "$SCRIPT_DIR/config.env"
  echo "config.env aangemaakt uit template — vul de API-keys in!"
fi
chmod 600 "$SCRIPT_DIR/config.env"

# --- sanity-waarschuwingen (geen fail: op sommige machines root-only leesbaar) ---
[ -e "$SECRETS_FILE" ] || echo "LET OP: $SECRETS_FILE bestaat niet"
[ -d "$MEDIA_ROOT" ]   || echo "LET OP: $MEDIA_ROOT bestaat niet"

# --- cron-suggesties met opgeloste paden ---
cat <<EOF

Voorgestelde cron-regels (crontab -e als de user die de media beheert):
  0 22,0,2,4,6 * * * /usr/bin/python3 $SCRIPT_DIR/arr_janitor.py all >> $SCRIPT_DIR/cron.log 2>&1
  0 5 * * * /usr/bin/python3 $SCRIPT_DIR/arr_janitor.py anime >> $SCRIPT_DIR/cron.log 2>&1
  30 5 * * * /usr/bin/python3 $SCRIPT_DIR/arr_janitor.py plexlang >> $SCRIPT_DIR/cron.log 2>&1
  0 5 * * * /usr/bin/python3 $SCRIPT_DIR/dv_guard.py >> $SCRIPT_DIR/cron.log 2>&1

Klaar.
EOF
