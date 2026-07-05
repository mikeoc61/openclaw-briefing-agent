#!/usr/bin/env python3
"""
Fetch today's events from iCloud CalDAV for the morning briefing.
Outputs JSON to stdout. Exits non-zero on failure.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import caldav
from caldav.lib.error import AuthorizationError
from icalendar import Calendar as ICal
import recurring_ical_events

import local_config

AUTH_PATH = Path(
    local_config.get("CALDAV_CREDS", "~/.openclaw/credentials/icloud.json")
).expanduser()
CALDAV_URL = "https://caldav.icloud.com"
TZ = ZoneInfo("Pacific/Honolulu")

# Calendar names are personal — configured in ~/.openclaw/briefing.env
INCLUDE = {
    c.strip() for c in local_config.get("CAL_INCLUDE", "").split(",") if c.strip()
}
WINDOW_DAYS = 1


def load_creds():
    with open(AUTH_PATH) as f:
        creds = json.load(f)
    return creds["username"], creds["app_password"]


def window_hst():
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=WINDOW_DAYS)
    return start, end


def format_time(dt):
    if isinstance(dt, datetime):
        return dt.astimezone(TZ).strftime("%H:%M HST")
    return "all-day"


def iso(dt):
    return dt.isoformat()


def fetch_events():
    username, password = load_creds()
    client = caldav.DAVClient(url=CALDAV_URL, username=username, password=password)
    principal = client.principal()
    window_start, window_end = window_hst()
    out = []

    for cal in principal.calendars():
        name = cal.name or ""
        if name not in INCLUDE:
            continue

        try:
            raw = cal.events()
        except Exception as e:
            out.append({
                "calendar": name,
                "title": f"(error fetching: {type(e).__name__})",
                "start": "all-day",
                "start_iso": window_start.isoformat(),
                "end": None, "location": None, "all_day": True,
            })
            continue

        ical = ICal()
        ical.add("prodid", "-//openclaw//briefing//EN")
        ical.add("version", "2.0")
        for ev in raw:
            for comp in ev.icalendar_instance.subcomponents:
                ical.add_component(comp)

        for occ in recurring_ical_events.of(ical).between(window_start, window_end):
            dtstart = occ.get("DTSTART").dt
            dtend_obj = occ.get("DTEND")
            dtend = dtend_obj.dt if dtend_obj else None
            all_day = not isinstance(dtstart, datetime)
            loc = occ.get("LOCATION")
            location = None
            if loc:
                first_line = str(loc).split("\n")[0].strip()
                location = first_line or None

            out.append({
                "calendar": name,
                "title": str(occ.get("SUMMARY", "(no title)")),
                "start": format_time(dtstart),
                "start_iso": iso(dtstart),
                "end": format_time(dtend) if dtend else None,
                "location": location,
                "all_day": all_day,
            })

    out.sort(key=lambda e: (not e["all_day"], e["start_iso"]))

    return {
        "events": out,
        "count": len(out),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "error": None,
    }


def main():
    try:
        result = fetch_events()
    except AuthorizationError:
        print(json.dumps({
            "events": [], "count": 0, "window_start": None, "window_end": None,
            "error": "auth_failed"
        }, indent=2))
        sys.exit(2)
    except Exception as e:
        print(json.dumps({
            "events": [], "count": 0, "window_start": None, "window_end": None,
            "error": f"{type(e).__name__}: {e}"
        }, indent=2))
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
