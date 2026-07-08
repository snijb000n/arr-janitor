#!/usr/bin/env bash
# One-shot fix voor de 4 anime-batches die vastzitten in Sonarr queue.
# Zie /root/.claude/plans/look-at-the-activity-spicy-scroll.md (Part B).
#
# Run als laominecon:
#   bash /home/laominecon/scripts/arr-janitor/fix-anime-batches.sh
#
# Wat dit script doet:
#  1. Zet seriesType=anime op WIND BREAKER (36), 7th Prince/Tensei (40), Haikyu (33).
#  2. Trigger RefreshSeries op die 3.
#  3. Hernoemt de 13 Tokyo Revengers BD batch files naar SxxExx-formaat.
#  4. Roept arr_janitor.py import aan zodat de files binnen worden gehaald.

set -euo pipefail

# Config laden
. /home/laominecon/scripts/arr-janitor/config.env
KEY="$SONARR_API_KEY"
URL="$SONARR_URL"

echo '=== Stap 1: seriesType=anime + RefreshSeries op sid 36, 40, 33 ==='
for sid in 36 40 33; do
  before=$(curl -s -H "X-Api-Key: $KEY" "$URL/api/v3/series/$sid" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('title'),'|',d.get('seriesType'))")
  echo "  before sid=$sid: $before"

  curl -s -H "X-Api-Key: $KEY" "$URL/api/v3/series/$sid" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); d['seriesType']='anime'; sys.stdout.write(json.dumps(d))" \
    | curl -s -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \
           -X PUT --data-binary @- "$URL/api/v3/series/$sid" > /dev/null

  after=$(curl -s -H "X-Api-Key: $KEY" "$URL/api/v3/series/$sid" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('seriesType'))")
  echo "  after  sid=$sid: seriesType=$after"

  curl -s -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \
       -X POST -d "{\"name\":\"RefreshSeries\",\"seriesId\":$sid}" \
       "$URL/api/v3/command" > /dev/null
  echo "         RefreshSeries triggered"
done

echo ''
echo '=== Stap 2: Tokyo Revengers BD batch hernoemen ==='
TR_DIR="/home/laominecon/compose/complete/downloads/completed/tv/[Anime Time] Tokyo Revengers  (Season 2) [BD] [Uncensored] [Dual Audio] [1080p][HEVC 10bit x265][AAC][Eng Sub] [Batch]"
if [ -d "$TR_DIR" ]; then
  cd "$TR_DIR"
  shopt -s nullglob
  for f in "[Anime Time] Tokyo Revengers Season 2 - "*.mkv; do
    num=$(echo "$f" | grep -oP 'Season 2 - \K\d+')
    newname="Tokyo.Revengers.S02E${num}.mkv"
    if [ ! -e "$newname" ]; then
      echo "  rename: $f"
      echo "       → $newname"
      mv -- "$f" "$newname"
    else
      echo "  skip (target exists): $newname"
    fi
  done
  shopt -u nullglob
else
  echo "  (folder niet gevonden, skip)"
fi

echo ''
echo '=== Stap 3: arr-janitor import draaien ==='
sleep 5   # geef Sonarr even om de refresh af te ronden
python3 /home/laominecon/scripts/arr-janitor/arr_janitor.py import --verbose

echo ''
echo '=== Klaar. Run "clean --verbose" zo om eventuele zombies op te ruimen. ==='
