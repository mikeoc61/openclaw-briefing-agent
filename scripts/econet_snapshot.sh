#!/usr/bin/env bash
# EcoNet Heat Pump Water Heater snapshot for Morning Intel Brief
# Reads Home Assistant token from a local-only file outside the mirrored workspace.
# Uses the same HA_TOKEN infrastructure as powerwall_snapshot.sh and blink_check.sh.

# No -e, matching powerwall_snapshot.sh: a failed step should still print an
# "unavailable" line for the composer instead of aborting with no output.
# Every fallible step below is checked explicitly.
set -uo pipefail

TOKEN_FILE="${HOME}/.openclaw/powerwall.token"
HA_URL_FILE="${HOME}/.config/openclaw/homeassistant.url"
DEFAULT_HA_URL="http://localhost:8123"
HA_MAX_TIME="${HA_MAX_TIME:-15}"

bail() {
  echo "Heat Pump Water Heater: unavailable ($1)"
  exit 0
}

[[ -r "$TOKEN_FILE" ]] || bail "missing token file: $TOKEN_FILE"

HA_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")" || bail "cannot read $TOKEN_FILE"
# A readable but empty token file otherwise yields a 401 and a dashed-out
# summary with nothing pointing at the cause.
[[ -n "$HA_TOKEN" ]] || bail "empty token file: $TOKEN_FILE"

HA_URL="$DEFAULT_HA_URL"
if [[ -r "$HA_URL_FILE" ]]; then
  ha_url_override="$(tr -d '\r\n' < "$HA_URL_FILE")"
  [[ -n "$ha_url_override" ]] && HA_URL="$ha_url_override"
fi

# One request for every entity; the eight lookups then happen locally. The
# per-entity form meant eight round trips and sixteen processes, and
# briefing_parent.sh waits on collectors with a bare `wait`, so a request
# that hangs holds up the whole briefing. --max-time bounds that.
if ! STATES="$(curl -fsS --max-time "$HA_MAX_TIME" \
    -H "Authorization: Bearer ${HA_TOKEN}" \
    "${HA_URL}/api/states" 2>/dev/null)"; then
  bail "no response from Home Assistant at ${HA_URL}"
fi

# Held in a variable so the JSON can arrive on stdin.
PYCODE="$(cat <<'PY'
import json
import re
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

MISSING = ("", "unavailable", "unknown")
found = []


def get(entity, default="—"):
    """State of entity, or default when absent, null or not yet known."""
    val = states.get(entity)
    if not isinstance(val, str) or val.strip().lower() in MISSING:
        found.append(False)
        return default
    found.append(True)
    return val.strip()


NUMERIC = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


def with_unit(val, unit):
    """Append a unit only to a number, so '—' does not become '—%'."""
    return f"{val}{unit}" if NUMERIC.match(val) else val


SENSOR = "sensor.heat_pump_water_heater_"
mode          = get("water_heater.heat_pump_water_heater", "unknown")
running       = get("binary_sensor.heat_pump_water_heater_running")
running_state = get(SENSOR + "running_state", "")
available_hot = get(SENSOR + "available_hot_water")
power_today   = get(SENSOR + "power_usage_today")
compressor    = get(SENSOR + "compressor_health")
tank          = get(SENSOR + "tank_health")
alerts        = get(SENSOR + "alert_count", "0")

# Home Assistant answered but knows none of these entities: the integration
# is down, which is a different fault from an unreachable Home Assistant.
if not any(found):
    print("Heat Pump Water Heater: unavailable (no water heater entities in Home Assistant)")
    sys.exit(0)

# Running state: only include if non-empty
running_part = f" ({running_state})" if running_state else ""

# Alert handling: warn prominently when alerts are active
try:
    alert_int = int(float(alerts))
except ValueError:
    alert_int = 0

if alert_int > 0:
    alert_note = f" ⚠️ {alert_int} active alert(s) — check EcoNet app; reported data may be incomplete."
else:
    alert_note = ""

summary = (
    f"Heat Pump Water Heater: Mode {mode}, "
    f"running {running}{running_part}, "
    f"{with_unit(available_hot, '%')} hot water available. "
    f"Power today: {with_unit(power_today, ' kWh')}. "
    f"Health: compressor {with_unit(compressor, '%')}, tank {with_unit(tank, '%')}."
    f"{alert_note}"
)

print(summary)
PY
)"

if ! summary="$(printf '%s' "$STATES" | python3 -c "$PYCODE" 2>/dev/null)" \
   || [[ -z "$summary" ]]; then
  bail "unreadable states response from ${HA_URL}"
fi

printf '%s\n' "$summary"
