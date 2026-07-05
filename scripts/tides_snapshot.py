#!/usr/bin/env python3
"""Tide extremes — tidecheck.com (token) with NOAA CO-OPS fallback."""
import json, urllib.request, datetime, zoneinfo, pathlib

def try_tidecheck():
    token_file = pathlib.Path.home() / '.openclaw/tidecheck.token'
    if not token_file.exists():
        return False
    
    token = token_file.read_text().strip()
    station_id = '1617846'
    tz = zoneinfo.ZoneInfo('Pacific/Honolulu')
    
    url = f'https://tidecheck.com/api/station/{station_id}/tides?datum=MLLW&days=1'
    headers = {
        'X-API-Key': token,
        'User-Agent': 'Mozilla/5.0'
    }
    req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        
        extremes = data.get('extremes') or []
        if extremes:
            output = []
            for extreme in extremes[:4]:
                time_str = extreme.get('time')
                height = extreme.get('height')
                event_type = extreme.get('type', '').lower()
                if time_str and height is not None and event_type:
                    try:
                        dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00')).astimezone(tz)
                        time_display = dt.strftime('%-I:%M %p')
                        event_label = 'High' if event_type == 'high' else 'Low'
                        output.append(f'{event_label} {time_display} ({height:.1f} ft)')
                    except:
                        pass
            if output:
                print(' | '.join(output))
                return True
        return False
    except Exception:
        return False

def try_noaa():
    tz = zoneinfo.ZoneInfo('Pacific/Honolulu')
    now = datetime.datetime.now(tz)
    today = now.strftime('%Y%m%d')
    next_day = (now + datetime.timedelta(days=1)).strftime('%Y%m%d')
    station = '1617760'
    url = f'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?station={station}&begin_date={today}&end_date={next_day}&product=predictions&datum=MLLW&units=english&time_zone=lst&format=json'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        predictions = data.get('predictions') or []
        if predictions:
            values = [(datetime.datetime.strptime(p['t'], '%Y-%m-%d %H:%M'), float(p['v'])) for p in predictions]
            extrema = []
            for i in range(2, len(values) - 2):
                v = [values[j][1] for j in range(i-2, i+3)]
                curr_val = v[2]
                if curr_val > max(v[0], v[1], v[3], v[4]):
                    extrema.append((values[i][0], curr_val, 'High'))
                elif curr_val < min(v[0], v[1], v[3], v[4]):
                    extrema.append((values[i][0], curr_val, 'Low'))
            if extrema:
                deduplicated = []
                for dt, val, event in sorted(extrema):
                    if deduplicated and deduplicated[-1][2] == event and (dt - deduplicated[-1][0]).total_seconds() < 3600:
                        if (event == 'High' and val > deduplicated[-1][1]) or (event == 'Low' and val < deduplicated[-1][1]):
                            deduplicated[-1] = (dt, val, event)
                    else:
                        deduplicated.append((dt, val, event))
                output = []
                for dt, val, event in deduplicated[:4]:
                    time_str = dt.strftime('%-I:%M %p')
                    output.append(f'{event} {time_str} ({val:.1f} ft)')
                print(' | '.join(output))
            else:
                print('Tides: unable to parse')
        else:
            print('Tides: unavailable')
    except Exception:
        print('Tides: unavailable')

if not try_tidecheck():
    try_noaa()
