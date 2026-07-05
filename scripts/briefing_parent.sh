#!/usr/bin/env bash
set -euo pipefail

# Morning Intel Brief runner.
# Single entrypoint: guards against duplicate runs, gathers raw collector
# outputs in parallel, composes the brief via compose_briefing.py, and
# delivers via msmtp (HTML email) + Signal.
#
# Layout:
#   collectors  → $TMPDIR/<name>.txt   (parallel, each fails soft)
#   composer    → scripts/compose_briefing.py "$TMPDIR"
#   html        → scripts/render_html.py
#
# Idempotency: a sent-marker in state/ is written ONLY after successful
# delivery. Re-invocations the same day (e.g. OpenClaw LLM-failover
# re-delivering the cron trigger) exit 0 without re-sending. A flock
# prevents two instances overlapping mid-run.

ROOT="${HOME}/.openclaw/workspace"
BRIEFING_DIR="${HOME}/.openclaw/workspace-briefing"

# Local-only personal config (delivery targets, home coords) — never in git.
# See briefing.env.example in the repo root.
BRIEFING_ENV="${HOME}/.openclaw/briefing.env"
if [[ -r "$BRIEFING_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$BRIEFING_ENV"
  set +a
else
  echo "ERROR: missing $BRIEFING_ENV — copy briefing.env.example and fill in values." >&2
  exit 1
fi
: "${BRIEFING_EMAIL_TO:?BRIEFING_EMAIL_TO not set in $BRIEFING_ENV}"
: "${BRIEFING_SIGNAL_TARGET:?BRIEFING_SIGNAL_TARGET not set in $BRIEFING_ENV}"
SCRIPTS="$BRIEFING_DIR/scripts"
STATE_DIR="$BRIEFING_DIR/state"
DATE_HST="$(TZ=Pacific/Honolulu date '+%A, %B %-d, %Y')"
TODAY="$(TZ=Pacific/Honolulu date '+%Y-%m-%d')"

mkdir -p "$STATE_DIR"

# ── Guard 1: concurrency lock (two instances running simultaneously) ────────
exec 9>"$STATE_DIR/briefing.lock"
if ! flock -n 9; then
  echo "Briefing run already in progress — exiting (no action needed)."
  exit 0
fi

# ── Guard 2: idempotency (already sent today) ───────────────────────────────
SENT_MARKER="$STATE_DIR/briefing_sent.$TODAY"
FORCE="${1:-}"
if [[ -f "$SENT_MARKER" && "$FORCE" != "--force" ]]; then
  echo "Briefing already sent today ($TODAY at $(cat "$SENT_MARKER" 2>/dev/null || echo '?')) — exiting (no action needed)."
  exit 0
fi

TMPDIR="$(mktemp -d -p /run/user/$(id -u))"
trap 'rm -rf "$TMPDIR"' EXIT

# ── Host-local collectors (no standalone script needed) ─────────────────────

system_health() {
  vcgencmd measure_temp && df -h / && free -h && uptime
}

gateway_status() {
  systemctl status openclaw-gateway --no-pager -l 2>/dev/null || true
  openclaw status 2>/dev/null || true
}

gateway_details() {
  # Current and latest versions
  CURRENT=$(openclaw --version 2>/dev/null || echo "unknown")
  LATEST=$(npm view openclaw version 2>/dev/null || echo "unknown")
  # Extract semver from e.g. "OpenClaw 2026.6.1 (2e08f0f)"
  CURRENT_VER=$(echo "$CURRENT" | grep -oP '\d+\.\d+\.\d+' || echo "$CURRENT")
  if [ "$CURRENT" != "unknown" ] && [ "$LATEST" != "unknown" ]; then
    echo "version_current=${CURRENT}"
    echo "version_latest=${LATEST}"
    if [ "$CURRENT_VER" = "$LATEST" ]; then
      echo "version_status=up-to-date"
    else
      echo "version_status=UPDATE AVAILABLE (latest: $LATEST)"
    fi
  else
    echo "version_current=${CURRENT}"
    echo "version_latest=${LATEST}"
    echo "version_status=check failed"
  fi

  # Default LLM provider and model from config
  python3 <<'PY' 2>/dev/null || { echo "llm_provider=unknown"; echo "llm_model=unknown"; return; }
import json, os
with open(os.path.expanduser("~/.openclaw/openclaw.json")) as f:
    d = json.load(f)
agents = d.get("agents", {})
defaults = agents.get("defaults", {})
default_model = defaults.get("model", "unknown")
# Extract provider from model string (e.g. deepseek/deepseek-v4-flash)
if "/" in default_model:
    provider, model_id = default_model.split("/", 1)
else:
    provider = "default"
    model_id = default_model
print(f"llm_provider={provider}")
print(f"llm_model={model_id}")
# Also check the briefing agent specific model
briefing_cfg = next((a for a in d.get("agents", {}).get("list", []) if a.get("id") == "briefing"), {})
primary = briefing_cfg.get("model", {}).get("primary", default_model)
fallbacks = briefing_cfg.get("model", {}).get("fallbacks", [])
print(f"briefing_model={primary}")
if fallbacks:
    print(f"briefing_fallback={', '.join(fallbacks)}")
PY
}

bitcoin_node() {
  /usr/local/bin/bitcoin-cli -conf="${HOME}/.bitcoin/bitcoin.conf" getblockchaininfo 2>/dev/null | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)))' 2>/dev/null || true
  /usr/local/bin/bitcoin-cli -conf="${HOME}/.bitcoin/bitcoin.conf" getnetworkinfo 2>/dev/null | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)))' 2>/dev/null || true
  /usr/local/bin/bitcoin-cli -conf="${HOME}/.bitcoin/bitcoin.conf" getmempoolinfo 2>/dev/null | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)))' 2>/dev/null || true
  # External reachability: probe public IP:8333 via nc
  PUBLIC_IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
  if [ -n "$PUBLIC_IP" ]; then
    if nc -zv -w 5 "$PUBLIC_IP" 8333 >/dev/null 2>&1; then
      echo "{\"public_ip\":\"${PUBLIC_IP}\",\"port_reachable\":true}"
    else
      echo "{\"public_ip\":\"${PUBLIC_IP}\",\"port_reachable\":false}"
    fi
  else
    echo "{\"public_ip\":null,\"port_reachable\":null}"
  fi
}

# ── Parallel collection ──────────────────────────────────────────────────────
# run <name> <fallback-text> <command...>
#   Backgrounds the command, writing stdout to $TMPDIR/<name>.txt.
#   On failure or empty output, writes the fallback text instead so the
#   composer always has a file to read. Never propagates failure (set -e safe).
run() {
  local name="$1" fallback="$2"
  shift 2
  {
    if ! "$@" > "$TMPDIR/$name.txt" 2>/dev/null || [[ ! -s "$TMPDIR/$name.txt" ]]; then
      printf '%s\n' "$fallback" > "$TMPDIR/$name.txt"
    fi
  } &
}

# run_stderr <name> <fallback> <command...>
#   Same as run(), but captures stderr into the output file too — for
#   collectors that report via stderr (e.g. blink_check.sh).
run_stderr() {
  local name="$1" fallback="$2"
  shift 2
  {
    if ! "$@" > "$TMPDIR/$name.txt" 2>&1 || [[ ! -s "$TMPDIR/$name.txt" ]]; then
      [[ -s "$TMPDIR/$name.txt" ]] || printf '%s\n' "$fallback" > "$TMPDIR/$name.txt"
    fi
  } &
}

# ── US market session status (drives conditional collection below) ──────────
# open | closed:weekend | closed:holiday:<name>
MARKET_STATUS="$(python3 "$SCRIPTS/market_calendar.py" 2>/dev/null || echo open)"
printf '%s\n' "$MARKET_STATUS" > "$TMPDIR/market_status.txt"

case "$MARKET_STATUS" in
  closed:weekend)
    CLOSED_MSG="US markets closed (weekend) — no session data"
    SKIP_MARKETS=1; SKIP_US_EQUITIES=1 ;;
  closed:holiday:*)
    # Intl equities, FX, and metals still trade on US holidays — keep markets
    CLOSED_MSG="US markets closed (${MARKET_STATUS#closed:holiday:}) — no session data"
    SKIP_MARKETS=0; SKIP_US_EQUITIES=1 ;;
  *)
    SKIP_MARKETS=0; SKIP_US_EQUITIES=0 ;;
esac

run weather          'Forecast: unavailable'          python3 "$SCRIPTS/weather_snapshot.py"
run sunrise          'Sunrise/sunset: unavailable'    python3 "$SCRIPTS/sun_snapshot.py"
run tides            'Tides: unavailable'             python3 "$SCRIPTS/tides_snapshot.py"
run powerwall        ''                               "$SCRIPTS/powerwall_snapshot.sh"
run econet           ''                               "$SCRIPTS/econet_snapshot.sh"
run health           ''                               system_health
run gateway          ''                               gateway_status
run gw_details       'version_status=check failed'    gateway_details
run bitcoin_node     ''                               bitcoin_node
run bitcoin_snapshot ''                               "$SCRIPTS/bitcoin_snapshot.sh"
run btc_sma          '200d SMA: unavailable'          "$SCRIPTS/btc_sma.sh"
if [[ "$SKIP_MARKETS" == 1 ]]; then
  printf '%s\n' "$CLOSED_MSG" > "$TMPDIR/markets.txt"
else
  run markets        ''                               "$SCRIPTS/market_snapshot.sh"
fi
run news             '- unavailable'                  python3 "$SCRIPTS/crypto_news.py"
if [[ "$SKIP_US_EQUITIES" == 1 ]]; then
  printf '%s\n' "$CLOSED_MSG" > "$TMPDIR/portfolio.txt"
  printf '%s\n' "$CLOSED_MSG" > "$TMPDIR/strc.txt"
else
  run portfolio      ''                               "$SCRIPTS/portfolio_snapshot.sh"
  run strc           'STRC: unavailable'              "$SCRIPTS/strc_snapshot.sh"
fi
run calendar         '{"error":"script failed"}'      "$SCRIPTS/calendar_snapshot.sh"
run fail2ban         '{"error":"script failed"}'      "$SCRIPTS/fail2ban_summary.sh"
run disk             '{"error":"disk_smart failed","disks":[]}' python3 "$SCRIPTS/disk_smart.py"

run_stderr blink 'Blink: check failed (no output)' "$SCRIPTS/blink_check.sh"

wait

# ── Compose ──────────────────────────────────────────────────────────────────
briefing="$(python3 "$SCRIPTS/compose_briefing.py" "$TMPDIR")"

if [[ -z "$briefing" ]]; then
  echo "ERROR: composer produced empty briefing — aborting before send." >&2
  exit 1
fi

printf '%s\n' "$briefing"
printf '%s\n' "$briefing" > "$TMPDIR/briefing_plain.txt"

# ── Deliver ──────────────────────────────────────────────────────────────────
html_briefing="$(python3 "$SCRIPTS/render_html.py" "$TMPDIR/briefing_plain.txt")"

# Note: literal — used in Subject — printf does not interpret \u Unicode escapes
printf "MIME-Version: 1.0\nContent-Type: text/html; charset=UTF-8\nSubject: Morning Intel Brief — %s\nTo: %s\nFrom: pi-briefing@localhost\n\n%s" "$DATE_HST" "$BRIEFING_EMAIL_TO" "$html_briefing" | msmtp "$BRIEFING_EMAIL_TO"

# Send plain-text briefing via Signal
openclaw message send --channel signal --target "$BRIEFING_SIGNAL_TARGET" --message "$briefing"

# ── Mark sent (only after both deliveries succeeded) ─────────────────────────
TZ=Pacific/Honolulu date '+%-I:%M %p' > "$SENT_MARKER"
find "$STATE_DIR" -name 'briefing_sent.*' -mtime +7 -delete 2>/dev/null || true

echo "Briefing sent and marker written ($SENT_MARKER)."
