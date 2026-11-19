#!/usr/bin/env python3
"""Weather snapshot for home location via Open-Meteo. Prints summary lines.

Coordinates come from ~/.openclaw/briefing.env (HOME_LAT/HOME_LON/HOME_ELEV_M)
— kept out of the public repo. See briefing.env.example.
"""
import datetime
import json
import subprocess
import urllib.request
import zoneinfo
import local_config

WMO = {
    0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Rime fog',
    51: 'Light drizzle', 53: 'Drizzle', 55: 'Dense drizzle',
    61: 'Light rain', 63: 'Moderate rain', 65: 'Heavy rain',
    80: 'Light showers', 81: 'Showers', 82: 'Heavy showers',
    95: 'Thunderstorm', 96: 'Thunderstorm/hail', 99: 'Thunderstorm/hail',
}

# Bounded curl budget: at worst ~40s, enforced twice (--retry-max-time for the
# retry loop, timeout= for the process) so a hung fallback cannot stall the
# briefing's `wait` indefinitely.
CURL_ARGS = [
    '--fail', '--silent', '--show-error', '--location',
    '--retry', '2', '--retry-delay', '1', '--retry-all-errors',
    '--retry-max-time', '35', '--connect-timeout', '10',
    '--max-time', '20',
]
CURL_TIMEOUT = 40
URLLIB_TIMEOUT = 20


def fetch_json(url):
    """Fetch Open-Meteo JSON, falling back to curl for TLS-path failures.

    urllib occasionally times out during the TLS handshake on the Pi even when
    the endpoint is healthy.  curl uses a different TLS/network stack and has
    proven reliable from the same host, so use it as a bounded fallback rather
    than dropping the weather section after one failed handshake.
    """
    req = urllib.request.Request(url, headers={'User-Agent': 'OpenClaw-Briefing/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=URLLIB_TIMEOUT) as response:
            return json.load(response)
    except Exception as urllib_error:
        try:
            result = subprocess.run(
                ['curl', *CURL_ARGS, url],
                check=True,
                capture_output=True,
                text=True,
                timeout=CURL_TIMEOUT,
            )
            return json.loads(result.stdout)
        except Exception as curl_error:
            urllib_detail = getattr(urllib_error, 'reason', urllib_error)
            if isinstance(curl_error, subprocess.CalledProcessError):
                curl_detail = (curl_error.stderr or '').strip() or f'exit {curl_error.returncode}'
            else:
                curl_detail = str(curl_error)
            raise RuntimeError(
                f'Open-Meteo fetch failed via urllib ({urllib_detail}); '
                f'curl fallback failed ({curl_detail})'
            ) from curl_error


def forecast_url(lat, lon, elev):
    """Open-Meteo request for today and tomorrow in local (Honolulu) time."""
    return (
        f'https://api.open-meteo.com/v1/forecast'
        f'?latitude={lat}&longitude={lon}&elevation={elev}'
        f'&hourly=temperature_2m,precipitation_probability,weathercode,relativehumidity_2m'
        f'&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,'
        f'precipitation_probability_max,weathercode'
        f'&temperature_unit=fahrenheit&windspeed_unit=mph&precipitation_unit=inch'
        f'&timezone=Pacific%2FHonolulu&forecast_days=2'
    )


def report(data, now, tz):
    """Render the three briefing lines from an Open-Meteo payload."""
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

    print(f'Now: {cur_temp:.0f}°F, {cur_cond}, {cur_rh}% RH | Today: {lo:.0f}–{hi:.0f}°F, {day_cond}')
    if precip_prob >= 20 or precip_in > 0:
        print(f'Rain: {precip_prob}% chance, {precip_in:.2f}" expected today')
    else:
        print(f'Rain: {precip_prob}% chance — likely dry')
    print(f'Tomorrow: {tmrw_lo:.0f}–{tmrw_hi:.0f}°F, {tmrw_cond}, {tmrw_prob}% rain')


def main():
    lat = local_config.get('HOME_LAT')
    lon = local_config.get('HOME_LON')
    elev = local_config.get('HOME_ELEV_M', '0')
    if not (lat and lon):
        print('Forecast: unavailable (HOME_LAT/HOME_LON not set in ~/.openclaw/briefing.env)')
        return 0

    try:
        tz = zoneinfo.ZoneInfo('Pacific/Honolulu')
        now = datetime.datetime.now(tz)
        report(fetch_json(forecast_url(lat, lon, elev)), now, tz)
    except Exception as e:
        print(f'Forecast: unavailable ({e})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
