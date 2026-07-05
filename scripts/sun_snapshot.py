#!/usr/bin/env python3
"""Sunrise/sunset via api.weather.gov astronomical data."""
import json, urllib.request, datetime, zoneinfo
import local_config

point = local_config.get('NWS_POINT')
if not point:
    print('Sunrise/sunset: unavailable (NWS_POINT not set in ~/.openclaw/briefing.env)')
    raise SystemExit(0)
url = f'https://api.weather.gov/points/{point}'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    props = data.get('properties') or {}
    astro = props.get('astronomicalData')
    if isinstance(astro, dict):
        sunrise = astro.get('sunrise')
        sunset = astro.get('sunset')
        if sunrise and sunset:
            tz = zoneinfo.ZoneInfo('Pacific/Honolulu')
            sunrise_dt = datetime.datetime.fromisoformat(sunrise).astimezone(tz)
            sunset_dt = datetime.datetime.fromisoformat(sunset).astimezone(tz)
            sunrise_str = sunrise_dt.strftime('%-I:%M %p')
            sunset_str = sunset_dt.strftime('%-I:%M %p')
            print(f'Sunrise: {sunrise_str} | Sunset: {sunset_str}')
        else:
            print('Sunrise/sunset: unavailable')
    else:
        print('Sunrise/sunset: unavailable')
except Exception:
    print('Sunrise/sunset: unavailable')
