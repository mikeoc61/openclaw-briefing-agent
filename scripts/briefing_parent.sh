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
#
# --dry-run exercises the whole pipeline -- collectors, composer, HTML render --
# and stops before delivery, so the brief can be checked without waiting for
# the scheduled run. It sends nothing, writes no sent-marker, takes its own
# lock, and leaves state/ untouched, so it can never mask or displace a real
# run. It is read-only, not inert: the collectors still hit their APIs and the
# composer still spends one LLM completion.

usage() {
  cat <<'USAGE'
Usage: briefing_parent.sh [--force] [--dry-run]

  --force     Send again even if today's brief was already delivered.
  --dry-run   Collect, compose and render, print the brief, then stop before
              delivery. Sends no email or Signal message, writes no
              sent-marker, and runs collectors against a throwaway copy of
              state/ so day-over-day baselines are left intact.

              Not free and not offline: it makes the same outbound calls the
              real run does, including one LLM completion in the composer.
USAGE
}

FORCE=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)   FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "briefing_parent.sh: unknown option '$1'" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

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
# Exported so child collectors inherit it rather than each re-deriving the
# same default (market_snapshot.sh, fail2ban_summary.sh use ${STATE_DIR:-...}).
export STATE_DIR="$BRIEFING_DIR/state"
DATE_HST="$(TZ=Pacific/Honolulu date '+%A, %B %-d, %Y')"
TODAY="$(TZ=Pacific/Honolulu date '+%Y-%m-%d')"

mkdir -p "$STATE_DIR"

# ── Guard 1: concurrency lock (two instances running simultaneously) ────────
# A dry run locks a different file, under /run/user rather than state/. Sharing
# the real lock would let a dry run make the scheduled run exit as "already in
# progress" and silently skip the day's delivery.
if [[ "$DRY_RUN" == 1 ]]; then
  LOCK_FILE="/run/user/$(id -u)/briefing.dry-run.lock"
else
  LOCK_FILE="$STATE_DIR/briefing.lock"
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Briefing run already in progress — exiting (no action needed)."
  exit 0
fi

# ── Guard 2: idempotency (already sent today) ───────────────────────────────
SENT_MARKER="$STATE_DIR/briefing_sent.$TODAY"
if [[ -f "$SENT_MARKER" && "$FORCE" != 1 && "$DRY_RUN" != 1 ]]; then
  echo "Briefing already sent today ($TODAY at $(cat "$SENT_MARKER" 2>/dev/null || echo '?')) — exiting (no action needed)."
  exit 0
fi

TMPDIR="$(mktemp -d -p /run/user/$(id -u))"
trap 'rm -rf "$TMPDIR"' EXIT

# Collectors write back into STATE_DIR as well as reading it: market_snapshot.sh
# keeps the spot metal prices that its day-over-day % is measured against,
# fail2ban_summary.sh and mem_snapshot.py keep counters. A dry run must not
# move those baselines, or the next real brief reports deltas against the dry
# run instead of against yesterday. Copy state/ and point the collectors at the
# copy: they read true prior values, so the preview stays faithful, and every
# write lands in $TMPDIR and goes away with it.
if [[ "$DRY_RUN" == 1 ]]; then
  cp -a "$STATE_DIR" "$TMPDIR/state"
  export STATE_DIR="$TMPDIR/state"

  # Two collectors cache outside state/, and both honour an override:
  # disk_smart.py's device-identity cache and powerwall_snapshot.sh's session
  # cookie. Copy and redirect those too, on the same read-real/write-throwaway
  # basis, so a dry run leaves nothing behind anywhere.
  DRY_DEVICE_CACHE="$TMPDIR/device_config.json"
  cp -a "${SYS_AGENT_DEVICE_CACHE:-$HOME/.config/sys_agent/device_config.json}" \
        "$DRY_DEVICE_CACHE" 2>/dev/null || true
  export SYS_AGENT_DEVICE_CACHE="$DRY_DEVICE_CACHE"

  DRY_PW_COOKIE="$TMPDIR/pw_cookie"
  cp -a "${PW_COOKIE:-$HOME/.openclaw/.pw_cookie}" "$DRY_PW_COOKIE" 2>/dev/null || true
  export PW_COOKIE="$DRY_PW_COOKIE"
fi

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
  # Extract version from e.g. "OpenClaw 2026.6.1 (2e08f0f)" or
  # "OpenClaw 2026.7.1-2 (0790d9f)" — keep any -suffix (build/pre-release),
  # since npm reports it and dropping it forces a false version mismatch.
  CURRENT_VER=$(echo "$CURRENT" | grep -oP '\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?' || echo "$CURRENT")
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
  PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 <<'PY' 2>/dev/null || { echo "llm_provider=unknown"; echo "llm_model=unknown"; return; }
import json, os
from model_config import model_spec, resolve_agent_model

with open(os.path.expanduser("~/.openclaw/openclaw.json")) as f:
    d = json.load(f)
agents = d.get("agents", {})
defaults = agents.get("defaults", {})
default_model, default_fallbacks = model_spec(defaults.get("model", "unknown"))
default_model = default_model or "unknown"
# Extract provider from model string (e.g. deepseek/deepseek-v4-flash)
if "/" in default_model:
    provider, model_id = default_model.split("/", 1)
else:
    provider = "default"
    model_id = default_model
print(f"llm_provider={provider}")
print(f"llm_model={model_id}")
# Also check the briefing agent specific model
primary, fallbacks = resolve_agent_model(d, "briefing")
primary = primary or default_model
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
run memstats         ''                               python3 "$SCRIPTS/mem_snapshot.py" "$STATE_DIR"
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
# Rendered before the dry-run exit so a dry run covers render_html.py too --
# it sits in the delivery path, and a failure there breaks the real send.
html_briefing="$(python3 "$SCRIPTS/render_html.py" "$TMPDIR/briefing_plain.txt")"

if [[ "$DRY_RUN" == 1 ]]; then
  cat >&2 <<EOF

── dry run: nothing delivered ──────────────────────────────────────────────
 would email to : $BRIEFING_EMAIL_TO
 subject        : Morning Intel Brief — $DATE_HST
 would Signal to: $BRIEFING_SIGNAL_TARGET
 html rendered  : ${#html_briefing} bytes
 sent-marker    : not written ($SENT_MARKER)
 state/         : untouched (collectors ran against a copy)
 caches         : untouched (device-identity cache, Powerwall cookie copied)
EOF
  exit 0
fi

# Note: literal — used in Subject — printf does not interpret \u Unicode escapes
printf "MIME-Version: 1.0\nContent-Type: text/html; charset=UTF-8\nSubject: Morning Intel Brief — %s\nTo: %s\nFrom: pi-briefing@localhost\n\n%s" "$DATE_HST" "$BRIEFING_EMAIL_TO" "$html_briefing" | msmtp "$BRIEFING_EMAIL_TO"

# Send plain-text briefing via Signal
openclaw message send --channel signal --target "$BRIEFING_SIGNAL_TARGET" --message "$briefing"

# ── Mark sent (only after both deliveries succeeded) ─────────────────────────
TZ=Pacific/Honolulu date '+%-I:%M %p' > "$SENT_MARKER"
find "$STATE_DIR" -name 'briefing_sent.*' -mtime +7 -delete 2>/dev/null || true

echo "Briefing sent and marker written ($SENT_MARKER)."
