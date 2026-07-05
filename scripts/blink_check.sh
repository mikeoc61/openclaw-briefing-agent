#!/bin/bash
# blink_check.sh — Check each Blink camera for battery, temp, or WiFi signal issues
set -euo pipefail

TOKEN_FILE="${HOME}/.openclaw/powerwall.token"
HA_URL_FILE="${HOME}/.config/openclaw/homeassistant.url"
DEFAULT_HA_URL="http://localhost:8123"

if [[ ! -r "$TOKEN_FILE" ]]; then
  echo "Blink Cameras: unavailable (missing token file: $TOKEN_FILE)"
  exit 0
fi

HA_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
HA_URL="$DEFAULT_HA_URL"
if [[ -r "$HA_URL_FILE" ]]; then
  HA_URL="$(tr -d '\r\n' < "$HA_URL_FILE")"
fi

#-----------------
CAMERAS=(driveway front_door makai north_lanai south_lanai)
TEMP_THRESHOLD=110   # °F
RSSI_THRESHOLD=-75   # dBm
ALERTS=()

get_state() {
  curl -s -H "Authorization: Bearer $HA_TOKEN" \
    "$HA_URL/api/states/$1" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])" \
    || echo "unavailable"
}

for cam in "${CAMERAS[@]}"; do
  battery=$(get_state "binary_sensor.${cam}_battery")
  temp=$(get_state "sensor.blink_${cam}_temperature")
  rssi=$(get_state "sensor.blink_${cam}_wi_fi_signal_strength")

  issues=()
  if [[ "$battery" == "on" ]];          then issues+=("low battery"); fi
  if [[ "$battery" == "unavailable" ]]; then issues+=("unreachable"); fi

  temp_int="${temp%.*}"
  if [[ "$temp_int" =~ ^-?[0-9]+$ ]] && (( temp_int > TEMP_THRESHOLD )); then
    issues+=("temp ${temp}°F")
  fi

  if [[ "$rssi" =~ ^-?[0-9]+$ ]] && (( rssi < RSSI_THRESHOLD )); then
    issues+=("RSSI ${rssi}dBm")
  fi

  if [[ ${#issues[@]} -gt 0 ]]; then
    ALERTS+=("$cam: $(IFS=', '; echo "${issues[*]}")")
  fi

done

if [[ ${#ALERTS[@]} -gt 0 ]]; then
  echo "⚠ Blink cameras:"
  for alert in "${ALERTS[@]}"; do echo "  - $alert"; done
fi
