#!/bin/bash
# Fetches today's events from iCloud CalDAV.
# Outputs JSON to stdout. Exits non-zero on failure.
set -euo pipefail

exec /usr/bin/python3 "${HOME}/.openclaw/workspace-briefing/scripts/calendar_snapshot.py"
