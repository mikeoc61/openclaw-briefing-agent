#!/usr/bin/env python3
"""Weather snapshot for home location via Open-Meteo. Prints summary lines.

Coordinates come from ~/.openclaw/briefing.env (HOME_LAT/HOME_LON/HOME_ELEV_M)
— kept out of the public repo. See briefing.env.example.
"""
import json, urllib.request, datetime, zoneinfo
import local_config

LAT = local_config.get('HOME_LAT')
LON = local_config.get('HOME_LON')
ELEV = local_config.get('HOME_ELEV_M', '0')
if not (LAT and LON):
    print('Forecast: unavailable (HOME_LAT/HOME_LON not set in ~/.openclaw/briefing.env)')
    raise SystemExit(0)

WMO = {
    0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Rime fog',
    51: 'Light drizzle', 53: 'Drizzle', 55: 'Dense drizzle',
    61: 'Light rain', 63: 'Moderate rain', 65: 'Heavy rain',
    80: 'Light showers', 81: 'Showers', 82: 'Heavy showers',
    95: 'Thunderstorm', 96: 'Thunderstorm/hail', 99: 'Thunderstorm/hail',
}

try:
    tz = zoneinfo.ZoneInfo('Pacific/Honolulu')
    now = datetime.datetime.now(tz)

    url = (
        f'https://api.open-meteo.com/v1/forecast'
        f'?latitude={LAT}&longitude={LON}&elevation={ELEV}'
        f'&hourly=temperature_2m,precipitation_probability,weathercode,relativehumidity_2m'
        f'&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,'
        f'precipitation_probability_max,weathercode'
        f'&temperature_unit=fahrenheit&windspeed_unit=mph&precipitation_unit=inch'
        f'&timezone=Pacific%2FHonolulu&forecast_days=2'
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)

    daily = data.get('daily', {})
    hourly = data.get('hourly', {})

    # Find current hour
    cur_hour = now.replace(minute=0, second=0, microsecond=0)
    times = [
        datetime.datetime.fromisoformat(t).replace(tzinfo=tz)
        for t in hourly.get('time', [])
    ]
    idx = next((i for i, t in enumerate(times) if t >= cur_hour), 0)

    cur_temp = hourly['temperature_2m'][idx]
    cur_prob = hourly['precipitation_probability'][idx]
    cur_code = hourly['weathercode'][idx]
    cur_rh   = hourly['relativehumidity_2m'][idx]
    cur_cond = WMO.get(cur_code, f'Code {cur_code}')

    hi   = daily['temperature_2m_max'][0]
    lo   = daily['temperature_2m_min'][0]
    precip_in   = daily['precipitation_sum'][0]
    precip_prob = daily['precipitation_probability_max'][0]
    day_code = daily['weathercode'][0]
    day_cond = WMO.get(day_code, f'Code {day_code}')

    tmrw_hi   = daily['temperature_2m_max'][1]
    tmrw_lo   = daily['temperature_2m_min'][1]
    tmrw_prob = daily['precipitation_probability_max'][1]
    tmrw_code = daily['weathercode'][1]
    tmrw_cond = WMO.get(tmrw_code, f'Code {tmrw_code}')

    print(f'Now: {cur_temp:.0f}\u00b0F, {cur_cond}, {cur_rh}% RH | Today: {lo:.0f}\u2013{hi:.0f}\u00b0F, {day_cond}')
    if precip_prob >= 20 or precip_in > 0:
        print(f'Rain: {precip_prob}% chance, {precip_in:.2f}" expected today')
    else:
        print(f'Rain: {precip_prob}% chance — likely dry')
    print(f'Tomorrow: {tmrw_lo:.0f}\u2013{tmrw_hi:.0f}\u00b0F, {tmrw_cond}, {tmrw_prob}% rain')
except Exception as e:
    print(f'Forecast: unavailable ({e})')
