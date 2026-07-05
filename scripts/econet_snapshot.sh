#!/usr/bin/env bash
# EcoNet Heat Pump Water Heater snapshot for Morning Intel Brief
# Reads Home Assistant token from a local-only file outside the mirrored workspace.
# Uses same HA_TOKEN infrastructure as powerwall_snapshot.sh

set -euo pipefail

TOKEN_FILE="${HOME}/.openclaw/powerwall.token"
HA_URL_FILE="${HOME}/.config/openclaw/homeassistant.url"
DEFAULT_HA_URL="http://localhost:8123"

if [[ ! -r "$TOKEN_FILE" ]]; then
  echo "Heat Pump: unavailable (missing token file: $TOKEN_FILE)"
  exit 0
fi

HA_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
HA_URL="$DEFAULT_HA_URL"
if [[ -r "$HA_URL_FILE" ]]; then
  HA_URL="$(tr -d '\r\n' < "$HA_URL_FILE")"
fi

get_state() {
  local entity_id="$1"
  curl -fsS \
    -H "Authorization: Bearer ${HA_TOKEN}" \
    -H "Content-Type: application/json" \
    "${HA_URL}/api/states/${entity_id}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['state'])" 2>/dev/null || echo "unavailable"
}

mode=$(get_state water_heater.heat_pump_water_heater)
running=$(get_state binary_sensor.heat_pump_water_heater_running)
running_state=$(get_state sensor.heat_pump_water_heater_running_state)
available_hot=$(get_state sensor.heat_pump_water_heater_available_hot_water)
power_today=$(get_state sensor.heat_pump_water_heater_power_usage_today)
compressor_health=$(get_state sensor.heat_pump_water_heater_compressor_health)
tank_health=$(get_state sensor.heat_pump_water_heater_tank_health)
alert_count=$(get_state sensor.heat_pump_water_heater_alert_count)

python3 - "$mode" "$running" "$running_state" "$available_hot" "$power_today" "$compressor_health" "$tank_health" "$alert_count" <<'PY'
import sys
mode, running, running_state, available_hot, power_today, compressor_health, tank_health, alert_count = sys.argv[1:9]

def clean(val, default="—"):
    return val.strip() if val.strip() not in ("", "unavailable", "unknown") else default

mode          = clean(mode, "unknown")
running       = clean(running, "—")
running_state = clean(running_state, "")
available_hot = clean(available_hot, "—")
power_today   = clean(power_today, "—")
compressor    = clean(compressor_health, "—")
tank          = clean(tank_health, "—")
alerts        = clean(alert_count, "0")

# Running state: only include if non-empty
running_part = f" ({running_state})" if running_state else ""

# Alert handling: warn prominently when alerts are active
try:
    alert_int = int(alerts)
except ValueError:
    alert_int = 0

if alert_int > 0:
    alert_note = f" ⚠️ {alert_int} active alert(s) — check EcoNet app; reported data may be incomplete."
else:
    alert_note = ""

summary = (
    f"Heat Pump Water Heater: Mode {mode}, "
    f"running {running}{running_part}, "
    f"{available_hot}% hot water available. "
    f"Power today: {power_today} kWh. "
    f"Health: compressor {compressor}%, tank {tank}%."
    f"{alert_note}"
)

print(summary)
PY
