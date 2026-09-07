#!/usr/bin/env bash
set -euo pipefail

# Mail a failure report for a systemd unit over the same msmtp path the morning
# brief delivers on. Wired up with OnFailure= in the units worth hearing about;
# invoked with the failed unit's name (systemd's %i).
#
# Install as /usr/local/libexec/openclaw-alert. Runs as the briefing user, not
# root: it needs ~/.msmtprc and ~/.openclaw/briefing.env, and needs no privilege
# of its own.
#
# Only fires when a unit RUNS and FAILS. A timer that was never enabled, or a
# host that was powered off, triggers nothing — the brief's staleness warning
# remains the backstop for that case.

unit="${1:?usage: openclaw-alert <unit-name>}"

BRIEFING_ENV="${HOME}/.openclaw/briefing.env"
if [[ -r "$BRIEFING_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$BRIEFING_ENV"
  set +a
fi
: "${BRIEFING_EMAIL_TO:?BRIEFING_EMAIL_TO not set in $BRIEFING_ENV}"

host="$(hostname -s)"
when="$(TZ=Pacific/Honolulu date '+%Y-%m-%d %H:%M %Z')"

# Reading another unit's state and journal as a non-root user needs membership
# in systemd-journal (or adm); both degrade to a note rather than failing the
# alert, since a truncated mail beats no mail.
status="$(systemctl status --no-pager --lines=0 "$unit" 2>&1 || true)"
logs="$(journalctl --no-pager --lines=40 --unit="$unit" 2>&1 || true)"

printf 'Subject: [%s] %s FAILED\nTo: %s\nFrom: pi-briefing@localhost\n\n%s failed at %s.\n\n--- systemctl status ---\n%s\n\n--- last 40 journal lines ---\n%s\n' \
  "$host" "$unit" "$BRIEFING_EMAIL_TO" "$unit" "$when" "$status" "$logs" \
  | msmtp "$BRIEFING_EMAIL_TO"
