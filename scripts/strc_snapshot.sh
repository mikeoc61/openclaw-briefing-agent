#!/usr/bin/env bash
# STRC snapshot for Morning Intel Briefing
# Strategy Inc. (ex-MicroStrategy) — digital credit market proxy
# Pulls price, volume, and 52-week range from Yahoo Finance

set -euo pipefail

python3 - <<'PY'
import json, urllib.request, math

TICKER = 'STRC'

# ── Fetch 10-day chart for price/volume history ──────────────────────────
url_10d = f'https://query2.finance.yahoo.com/v8/finance/chart/{TICKER}?interval=1d&range=10d'
req = urllib.request.Request(url_10d, headers={
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'Accept': 'application/json'
})

try:
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    result = d['chart']['result'][0]
    meta = result['meta']
    closes = result['indicators']['quote'][0].get('close', [])
    volumes = result['indicators']['quote'][0].get('volume', [])
    clean_closes = [x for x in closes if isinstance(x, (int, float)) and math.isfinite(x)]
    clean_volumes = [v for v in volumes if isinstance(v, (int, float)) and math.isfinite(v) and v > 0]

    price = meta.get('regularMarketPrice')
    prev = clean_closes[-2] if len(clean_closes) >= 2 else meta.get('chartPreviousClose')

    if price is None or prev is None:
        raise ValueError('missing price data')

    chg = price - prev
    pct = (chg / prev) * 100
    arrow = '\u25b2' if chg >= 0 else '\u25bc'

    # Today's volume (last bar) and recent average
    today_vol = clean_volumes[-1] if clean_volumes else 0
    # Exclude today for avg (it may still be partial)
    recent_vols = clean_volumes[:-1] if len(clean_volumes) > 1 else clean_volumes
    avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    # 52-week range from meta
    hi52 = meta.get('fiftyTwoWeekHigh')
    lo52 = meta.get('fiftyTwoWeekLow')

    # Format volume compactly
    def fmt_vol(v):
        if v >= 1_000_000:
            return f'{v/1_000_000:.1f}M'
        elif v >= 1_000:
            return f'{v/1_000:.0f}K'
        return f'{v:.0f}'

    # Build summary line
    parts = [f'STRC (Strategy Inc.): ${price:.2f} {arrow} {pct:+.2f}%']
    parts.append(f'Vol: {fmt_vol(today_vol)} ({vol_ratio:.1f}x avg)')

    if hi52 and lo52:
        range_pct = ((price - lo52) / (hi52 - lo52)) * 100
        parts.append(f'52w: ${lo52:.2f} – ${hi52:.2f} ({range_pct:.0f}% from low)')

    print(' | '.join(parts))

    # Machine-parseable key=value lines for the Python summarizer
    print(f'strc_price={price}')
    print(f'strc_pct={pct}')
    print(f'strc_arrow={arrow}')
    print(f'strc_vol={today_vol}')
    print(f'strc_avg_vol={avg_vol}')
    print(f'strc_vol_ratio={vol_ratio}')
    if hi52: print(f'strc_hi52={hi52}')
    if lo52: print(f'strc_lo52={lo52}')

except Exception as e:
    print(f'STRC: unavailable ({e})')
    print('strc_price=')
    print('strc_pct=')
    print('strc_vol=')
PY
