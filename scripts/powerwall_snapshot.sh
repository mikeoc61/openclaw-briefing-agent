#!/usr/bin/env bash
# Powerwall / Home Assistant snapshot for Morning Intel Brief
# Reads Home Assistant token from a local-only file outside the mirrored workspace.

set -euo pipefail

TOKEN_FILE="${HOME}/.openclaw/powerwall.token"
HA_URL_FILE="${HOME}/.config/openclaw/homeassistant.url"
DEFAULT_HA_URL="http://localhost:8123"

if [[ ! -r "$TOKEN_FILE" ]]; then
  echo "Powerwall: unavailable (missing token file: $TOKEN_FILE)"
  exit 0
fi

HA_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
HA_URL="$DEFAULT_HA_URL"
if [[ -r "$HA_URL_FILE" ]]; then
  HA_URL="$(tr -d '\r\n' < "$HA_URL_FILE")"
fi

# Entity prefix is site-specific (HA derives it from the Powerwall site name)
# — configured in ~/.openclaw/briefing.env, not hardcoded.
BRIEFING_ENV="${HOME}/.openclaw/briefing.env"
if [[ -z "${HA_POWERWALL_PREFIX:-}" && -r "$BRIEFING_ENV" ]]; then
  HA_POWERWALL_PREFIX="$(grep -E '^HA_POWERWALL_PREFIX=' "$BRIEFING_ENV" | tail -1 | cut -d= -f2- | tr -d '"'"'"'')"
fi
if [[ -z "${HA_POWERWALL_PREFIX:-}" ]]; then
  echo "Powerwall: unavailable (HA_POWERWALL_PREFIX not set in $BRIEFING_ENV)"
  exit 0
fi

get_state() {
  local entity_id="$1"
  curl -fsS \
    -H "Authorization: Bearer ${HA_TOKEN}" \
    -H "Content-Type: application/json" \
    "${HA_URL}/api/states/${entity_id}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['state'])" 2>/dev/null || echo "unavailable"
}

charge="$(get_state "sensor.${HA_POWERWALL_PREFIX}_charge")"
backup_reserve="$(get_state "sensor.${HA_POWERWALL_PREFIX}_backup_reserve")"
load_power="$(get_state "sensor.${HA_POWERWALL_PREFIX}_load_power")"
battery_power="$(get_state "sensor.${HA_POWERWALL_PREFIX}_battery_power")"
grid_status="$(get_state "binary_sensor.${HA_POWERWALL_PREFIX}_grid_status")"
off_grid="$(get_state "switch.${HA_POWERWALL_PREFIX}_off_grid_operation")"
site_power="$(get_state "sensor.${HA_POWERWALL_PREFIX}_site_power")"

python3 - "$charge" "$backup_reserve" "$load_power" "$battery_power" "$grid_status" "$off_grid" "$site_power" <<'PY'
import sys
charge, reserve, load, battery, grid, offgrid, site = sys.argv[1:8]

try:
    site_val = float(site)
except Exception:
    site_val = None

if site_val is None:
    site_text = f"site power is {site} kW"
elif abs(site_val) < 0.1:
    site_text = "site power is basically flat near zero"
elif site_val < 0:
    site_text = f"site power shows export of {abs(site_val):.2f} kW"
else:
    site_text = f"site power shows import of {site_val:.2f} kW"

print(
    f"Powerwall: Battery at {charge}% with a {reserve}% backup reserve. "
    f"Home load is about {load} kW and the battery is supplying {battery} kW; "
    f"grid is {grid}, off-grid mode is {offgrid}, and {site_text}."
)
PY
