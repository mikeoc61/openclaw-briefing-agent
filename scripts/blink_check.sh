#!/bin/bash
# blink_check.sh — Check each Blink camera for battery, temp, or WiFi signal issues
# Uses the same HA_TOKEN infrastructure as econet_snapshot.sh.

# No -e: a failed step should still print a line for the composer instead of
# aborting with no output. Every fallible step below is checked explicitly.
set -uo pipefail

TOKEN_FILE="${HOME}/.openclaw/powerwall.token"
HA_URL_FILE="${HOME}/.config/openclaw/homeassistant.url"
DEFAULT_HA_URL="http://localhost:8123"
HA_MAX_TIME="${HA_MAX_TIME:-15}"

# briefing_parent.sh runs this with run_stderr, which folds stderr into the
# briefing body, so failures have to be reported as a clean line on stdout.
bail() {
  echo "Blink Cameras: unavailable ($1)"
  exit 0
}

[[ -r "$TOKEN_FILE" ]] || bail "missing token file: $TOKEN_FILE"

HA_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")" || bail "cannot read $TOKEN_FILE"
[[ -n "$HA_TOKEN" ]] || bail "empty token file: $TOKEN_FILE"

HA_URL="$DEFAULT_HA_URL"
if [[ -r "$HA_URL_FILE" ]]; then
  ha_url_override="$(tr -d '\r\n' < "$HA_URL_FILE")"
  [[ -n "$ha_url_override" ]] && HA_URL="$ha_url_override"
fi

#-----------------
CAMERAS=(driveway front_door makai north_lanai south_lanai)
TEMP_THRESHOLD=110   # °F
RSSI_THRESHOLD=-75   # dBm
ALERTS=()

# One request for every entity; the fifteen lookups then happen locally. The
# per-entity form meant fifteen round trips with no timeout, and
# briefing_parent.sh waits on collectors with a bare `wait`, so a request
# that hangs holds up the whole briefing. --max-time bounds that.
if ! STATES="$(curl -fsS --max-time "$HA_MAX_TIME" \
    -H "Authorization: Bearer $HA_TOKEN" \
    "$HA_URL/api/states" 2>/dev/null)"; then
  bail "no response from Home Assistant at $HA_URL"
fi

# Held in a variable so the JSON can arrive on stdin. Emits one TSV row per
# camera: name, battery, temperature, signal strength. An entity that is
# absent or null reads as "unavailable", which is what the per-entity form
# produced on a 404 -- but without the json.load traceback that used to land
# in the briefing body.
PYCODE="$(cat <<'PY'
import json
import sys

try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(doc, list):
    sys.exit(1)

states = {}
for item in doc:
    if isinstance(item, dict):
        entity = item.get("entity_id")
        if isinstance(entity, str):
            states[entity] = item.get("state")


def get(entity):
    val = states.get(entity)
    if not isinstance(val, str) or not val.strip():
        return "unavailable"
    return val.strip()


for cam in sys.argv[1:]:
    sys.stdout.write("\t".join([
        cam,
        get("binary_sensor.%s_battery" % cam),
        get("sensor.blink_%s_temperature" % cam),
        get("sensor.blink_%s_wi_fi_signal_strength" % cam),
    ]) + "\n")
PY
)"

if ! ROWS="$(printf '%s' "$STATES" | python3 -c "$PYCODE" "${CAMERAS[@]}" 2>/dev/null)" \
   || [[ -z "$ROWS" ]]; then
  bail "unreadable states response from $HA_URL"
fi

while IFS=$'\t' read -r cam battery temp rssi; do
  [[ -n "$cam" ]] || continue

  issues=()
  if [[ "$battery" == "on" ]];          then issues+=("low battery"); fi
  if [[ "$battery" == "unavailable" ]]; then issues+=("unreachable"); fi

  temp_int="${temp%.*}"
  if [[ "$temp_int" =~ ^-?[0-9]+$ ]] && (( temp_int > TEMP_THRESHOLD )); then
    issues+=("temp ${temp}°F")
  fi

  # Strip any fraction, as for temp: bash arithmetic is integer-only, and an
  # unstripped "-81.0" would fail the match and skip the check silently.
  rssi_int="${rssi%.*}"
  if [[ "$rssi_int" =~ ^-?[0-9]+$ ]] && (( rssi_int < RSSI_THRESHOLD )); then
    issues+=("RSSI ${rssi}dBm")
  fi

  if [[ ${#issues[@]} -gt 0 ]]; then
    # "${issues[*]}" joins on only the first character of IFS, so IFS=", "
    # gave "low battery,temp ..."; prefix each item with ", " and drop the first.
    joined="$(printf ', %s' "${issues[@]}")"
    ALERTS+=("$cam: ${joined:2}")
  fi

done <<< "$ROWS"

if [[ ${#ALERTS[@]} -gt 0 ]]; then
  echo "⚠ Blink cameras:"
  for alert in "${ALERTS[@]}"; do echo "  - $alert"; done
else
  echo "No Battery, Temp or Signal Strength issues"
fi
