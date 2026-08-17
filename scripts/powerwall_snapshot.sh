#!/usr/bin/env bash

set -uo pipefail

PW_ENV="${HOME}/.openclaw/powerwall.env"
if [[ -r "$PW_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PW_ENV"
  set +a
fi

PW_HOST="${PW_HOST:-192.168.7.66}"
PW_PASSWORD="${PW_PASSWORD:-}"
PW_EMAIL="${PW_EMAIL:-}"
PW_COOKIE="${PW_COOKIE:-${HOME}/.openclaw/.pw_cookie}"
PW_ALERT_EXIT="${PW_ALERT_EXIT:-0}"
PW_NAMEPLATE_WH="${PW_NAMEPLATE_WH:-13500}"

bail() {
  echo "Powerwall: unavailable ($1)"
  exit 0
}

if [[ -z "$PW_PASSWORD" ]]; then
  bail "PW_PASSWORD not set in $PW_ENV"
fi

api() {
  curl -sk --max-time 10 -b "$PW_COOKIE" "https://${PW_HOST}/api/$1"
}

login() {
  umask 077
  curl -sk --max-time 10 -c "$PW_COOKIE" -X POST \
    "https://${PW_HOST}/api/login/Basic" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"customer\",\"email\":\"${PW_EMAIL}\",\"password\":\"${PW_PASSWORD}\",\"force_sm_off\":false}" \
    >/dev/null 2>&1
}

if ! curl -sk --max-time 10 "https://${PW_HOST}/api/status" >/dev/null 2>&1; then
  bail "gateway ${PW_HOST} unreachable"
fi

SYS="$(api system_status)"
if ! printf '%s' "$SYS" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if "battery_blocks" in d else 1)' 2>/dev/null; then
  login
  SYS="$(api system_status)"
fi

AGG="$(api meters/aggregates)"
GRID="$(api 'system_status/grid_status')"
OP="$(api operation)"

for blob in "$AGG" "$SYS" "$GRID" "$OP"; do
  [[ -z "$blob" ]] && bail "empty response from gateway"
done

python3 - "$AGG" "$SYS" "$GRID" "$OP" "$PW_NAMEPLATE_WH" "$PW_ALERT_EXIT" <<'PY'
import json, sys

CURTAIL_HZ = 60.10
HUNT_HZ = 60.25
IDLE_W = 50.0

try:
    agg = json.loads(sys.argv[1])
    sysst = json.loads(sys.argv[2])
    grid = json.loads(sys.argv[3])
    op = json.loads(sys.argv[4])
except Exception as e:
    print(f"Powerwall: unavailable (bad JSON: {e})")
    sys.exit(0)

nameplate = float(sys.argv[5])
alert_exit = sys.argv[6] == "1"

def kw(w):
    return w / 1000.0

def p(meter, key="instant_power"):
    return float(agg.get(meter, {}).get(key) or 0.0)

solar = p("solar")
load = p("load")
batt = p("battery")
site = p("site")

sfreq = float(agg.get("solar", {}).get("frequency") or 0.0)
bfreq = float(agg.get("battery", {}).get("frequency") or 0.0)
freq = sfreq if (solar > 100 and sfreq > 30) else bfreq

rem = sysst.get("nominal_energy_remaining")
full = sysst.get("nominal_full_pack_energy")
blocks = len(sysst.get("battery_blocks") or [])

gstat = grid.get("grid_status", "unknown")
islanded = gstat != "SystemGridConnected"
reserve = op.get("backup_reserve_percent")
mode = op.get("real_mode", "unknown")

alert = 0
lines = []

if rem and full:
    soc = rem * 100.0 / full
    soh = full * 100.0 / nameplate
    cap = f"{soc:.1f}% ({kw(rem):.2f} of {kw(full):.2f} kWh, SoH {soh:.1f}%)"
else:
    cap = "capacity unavailable"

if batt < -IDLE_W:
    flow = f"charging at {kw(-batt):.2f} kW"
elif batt > IDLE_W:
    flow = f"discharging at {kw(batt):.2f} kW"
else:
    flow = "idle"

pack = f"{blocks} unit" if blocks == 1 else f"{blocks} units"
lines.append(f"Powerwall: {cap}, {flow} [{pack}].")

if islanded:
    site_txt = "islanded"
elif site < -IDLE_W:
    site_txt = f"exporting {kw(-site):.2f} kW"
elif site > IDLE_W:
    site_txt = f"importing {kw(site):.2f} kW"
else:
    site_txt = "grid neutral"

lines.append(
    f"Solar {kw(solar):.2f} kW, load {kw(load):.2f} kW, "
    f"surplus {kw(solar - load):+.2f} kW, {site_txt}."
)

if islanded:
    if freq > HUNT_HZ:
        fstate = "HUNTING - raise household load now"
        alert = 2
    elif freq > CURTAIL_HZ:
        fstate = "curtailing - surplus discarded, add load"
        alert = 1
    else:
        fstate = "unrestricted"
    lines.append(f"Grid DOWN ({gstat}), mode {mode}, {freq:.3f} Hz - {fstate}.")
else:
    lines.append(f"Grid up, mode {mode}, reserve {reserve}%.")

if rem and full:
    if islanded and batt > IDLE_W and solar < 200:
        lines.append(f"Runtime at current draw: {rem / batt:.1f} h.")
    elif batt < -IDLE_W and rem < full:
        lines.append(f"Time to full at current rate: {(full - rem) / -batt:.1f} h.")
    elif islanded:
        lines.append(f"Reserve: {kw(rem):.2f} kWh banked.")

print("\n".join(lines))
sys.exit(alert if alert_exit else 0)
PY
