#!/usr/bin/env bash
# fail2ban_summary.sh — sudo-free JSON summary of fail2ban activity
#
# Derives all data from the log file, which is readable by the adm group.
# No fail2ban-client or sudo required — safe to run from sandboxed contexts.
#
# Output: single-line JSON object with current state + 24h activity

JAIL="${JAIL:-sshd}"
SINCE="${SINCE:-24 hours ago}"
LOG_FILE="${LOG_FILE:-/var/log/fail2ban.log}"
STATE_DIR="${STATE_DIR:-$HOME/.openclaw/workspace-briefing/state}"
STATE_FILE="${STATE_DIR}/fail2ban_${JAIL}.last"

mkdir -p "$STATE_DIR"

if [[ ! -r "$LOG_FILE" ]]; then
  echo '{"error":"log file not readable","jail":"'"$JAIL"'","log":"'"$LOG_FILE"'"}'
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Parse Ban / Unban events from log to determine currently banned IPs
#    Format: YYYY-MM-DD HH:MM:SS,mmm fail2ban.actions [PID]: NOTICE  [JAIL] Ban <IP>
#            YYYY-MM-DD HH:MM:SS,mmm fail2ban.actions [PID]: NOTICE  [JAIL] Unban <IP>
# ---------------------------------------------------------------------------
BANNED_NOW=0
BANNED_IPS=""

# Track Ban/Unban pairs to find currently banned IPs
_TMPBAN=$(mktemp)
grep -E "fail2ban\.actions.*NOTICE.*\[${JAIL}\] (Ban|Unban) " "$LOG_FILE" > "$_TMPBAN"
BANNED_IPS_LIST=$(python3 - "$_TMPBAN" <<'PY'
import sys, re
banned = set()
ip_re = re.compile(r'(\d+\.\d+\.\d+\.\d+)')
for line in open(sys.argv[1]):
    m = ip_re.search(line)
    if not m:
        continue
    ip = m.group(1)
    if '] Ban ' in line:
        banned.add(ip)
    elif '] Unban ' in line:
        banned.discard(ip)
print(','.join(sorted(banned)))
PY
)
rm -f "$_TMPBAN"
BANNED_IPS="${BANNED_IPS_LIST:-none}"
if [[ -z "$BANNED_IPS" || "$BANNED_IPS" == "," ]]; then
  BANNED_IPS="none"
  BANNED_NOW=0
else
  BANNED_NOW=$(echo "$BANNED_IPS" | tr ',' '\n' | grep -c '.' || echo 0)
fi

# ---------------------------------------------------------------------------
# 2. 24h ban events
# ---------------------------------------------------------------------------
CUTOFF=$(date -d "$SINCE" '+%Y-%m-%d %H:%M:%S')

TOTAL_24H=$(awk -v cutoff="$CUTOFF" \
  '$0 >= cutoff && /fail2ban\.actions.*NOTICE.*\] Ban / {count++}
   END {print count+0}' "$LOG_FILE")

TOP_OFFENDERS=$(awk -v cutoff="$CUTOFF" \
  '$0 >= cutoff && /fail2ban\.actions.*NOTICE.*\] Ban / {
     ip = ""
     for (i=1; i<=NF; i++) if ($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) { ip = $i; break }
     if (ip) count[ip]++
   }
   END {
     for (ip in count) print count[ip], ip
   }' "$LOG_FILE" \
  | sort -rn | head -5 \
  | awk '{printf "%s (%s);", $2, $1}' | sed 's/;$//')

# ---------------------------------------------------------------------------
# 3. Lifetime totals from log (since last rollover)
# ---------------------------------------------------------------------------
TOTAL_FAILED=$(grep -cE "fail2ban\.filter.*INFO.*\[${JAIL}\] Found " "$LOG_FILE" || true)
TOTAL_FAILED=${TOTAL_FAILED:-0}
TOTAL_BANNED=$(grep -cE "fail2ban\.actions.*NOTICE.*\[${JAIL}\] Ban " "$LOG_FILE" || true)
TOTAL_BANNED=${TOTAL_BANNED:-0}

# ---------------------------------------------------------------------------
# 4. Delta since last run
# ---------------------------------------------------------------------------
PREV_TOTAL_FAILED=0
PREV_TOTAL_BANNED=0
PREV_TIMESTAMP="never"

if [[ -r "$STATE_FILE" ]]; then
  IFS='|' read -r PREV_TIMESTAMP PREV_TOTAL_FAILED PREV_TOTAL_BANNED < "$STATE_FILE" || true
  PREV_TOTAL_FAILED="${PREV_TOTAL_FAILED:-0}"
  PREV_TOTAL_BANNED="${PREV_TOTAL_BANNED:-0}"
fi

DELTA_FAILED=$(( TOTAL_FAILED - PREV_TOTAL_FAILED ))
DELTA_BANNED=$(( TOTAL_BANNED - PREV_TOTAL_BANNED ))
(( DELTA_FAILED < 0 )) && DELTA_FAILED=0
(( DELTA_BANNED < 0 )) && DELTA_BANNED=0

NOW=$(date '+%Y-%m-%dT%H:%M:%S%z')
echo "${NOW}|${TOTAL_FAILED}|${TOTAL_BANNED}" > "$STATE_FILE"

# ---------------------------------------------------------------------------
# 5. Emit JSON
# ---------------------------------------------------------------------------
cat <<EOF
{
  "jail": "${JAIL}",
  "source": "logfile",
  "timestamp": "${NOW}",
  "current": {
    "banned_now": ${BANNED_NOW},
    "banned_ips": "${BANNED_IPS:-none}"
  },
  "lifetime": {
    "total_failed": ${TOTAL_FAILED},
    "total_banned": ${TOTAL_BANNED}
  },
  "activity_24h": {
    "bans": ${TOTAL_24H},
    "top_offenders": "${TOP_OFFENDERS:-none}"
  },
  "delta_since_last_run": {
    "previous_run": "${PREV_TIMESTAMP}",
    "new_failures": ${DELTA_FAILED},
    "new_bans": ${DELTA_BANNED}
  }
}
EOF
