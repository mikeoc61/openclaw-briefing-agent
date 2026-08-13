#!/usr/bin/env bash
# Market snapshot for Morning Intel Briefing
# Pulls major indexes, DXY, commodities and US 10yr yield from Yahoo Finance

set -euo pipefail
UA="Mozilla/5.0"
BASE="https://query1.finance.yahoo.com/v8/finance/chart"

fetch() {
  local ticker=$1
  python3 - "$ticker" <<'PY'
import sys, json, urllib.request, math
label_map = {
  '%5EGSPC': 'S&P 500',
  '%5EIXIC': 'Nasdaq',
  '%5EDJI': 'Dow Jones',
  '%5EFTSE': 'FTSE 100',
  '%5EN225': 'Nikkei',
  'DX-Y.NYB': 'DXY',
  'GC%3DF': 'Gold (futures)',
  'SI%3DF': 'Silver (futures)',
  'HG%3DF': 'Copper',
  '%5ETNX': 'US 10yr yield',
}
ticker = sys.argv[1]
label = label_map.get(ticker, ticker)
url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    result = d['chart']['result'][0]
    m = result['meta']
    quote = result['indicators']['quote'][0]

    price = m.get('regularMarketPrice')
    closes = quote.get('close') or []
    clean_closes = [x for x in closes if isinstance(x, (int, float)) and math.isfinite(x)]

    prev = None
    if len(clean_closes) >= 2:
        prev = clean_closes[-2]
    elif m.get('chartPreviousClose') not in (None, 0):
        prev = m.get('chartPreviousClose')
    elif m.get('previousClose') not in (None, 0):
        prev = m.get('previousClose')

    if price in (None, 0) or prev in (None, 0):
        raise ValueError('missing price baseline')

    chg = price - prev
    pct = (chg / prev) * 100
    arrow = '▲' if chg >= 0 else '▼'

    if ticker == '%5ETNX':
        # price and prev are already in percent (e.g. 4.320 = 4.320%);
        # change in basis points = (close - prev_close) × 100
        bps = (price - prev) * 100
        bps_arrow = '▲' if bps >= 0 else '▼'
        print(f"- {label}: {price:.3f}% {bps_arrow} {bps:+.1f}bps")
    elif ticker in ('GC%3DF', 'SI%3DF', 'HG%3DF'):
        print(f"- {label}: ${price:,.2f} {arrow} {pct:+.2f}%")
    else:
        print(f"- {label}: {price:,.3f} {arrow} {pct:+.2f}%")
except Exception:
    print(f"- {label}: unavailable")
PY
}

fx() {
  python3 - <<'PY'
import json, urllib.request
req = urllib.request.Request('https://open.er-api.com/v6/latest/USD', headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    r = d['rates']
    # API returns units-per-USD; invert EUR and GBP to get conventional quote direction
    eur_usd = 1 / r['EUR'] if r.get('EUR') else None
    gbp_usd = 1 / r['GBP'] if r.get('GBP') else None
    usd_jpy = r.get('JPY')
    usd_chf = r.get('CHF')
    print(f"- EUR/USD: {eur_usd:.4f}" if eur_usd else '- EUR/USD: unavailable')
    print(f"- GBP/USD: {gbp_usd:.4f}" if gbp_usd else '- GBP/USD: unavailable')
    print(f"- USD/JPY: {usd_jpy:.4f}" if usd_jpy else '- USD/JPY: unavailable')
    print(f"- USD/CHF: {usd_chf:.4f}" if usd_chf else '- USD/CHF: unavailable')
except Exception:
    print('- EUR/USD: unavailable')
    print('- GBP/USD: unavailable')
    print('- JPY/USD: unavailable')
    print('- CHF/USD: unavailable')
PY
}

spot_metals() {
  # Spot XAU/XAG. Primary: Swissquote public feed. Fallback: gold-api.com.
  # Neither provides history, so day-over-day % comes from spot_metals.json
  # written on the previous run (first run prints price with no % change).
  # State dir matches fail2ban_summary.sh convention; override with STATE_DIR.
  python3 - "${STATE_DIR:-$HOME/.openclaw/workspace-briefing/state}" <<'PY'
import json, math, os, sys, time, urllib.request

state_dir = sys.argv[1]
state_path = os.path.join(state_dir, 'spot_metals.json')

def _get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def swissquote(sym):
    d = _get_json(f'https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{sym}/USD')
    mids = []
    for platform in d:
        for sp in platform.get('spreadProfilePrices', []):
            bid, ask = sp.get('bid'), sp.get('ask')
            if bid and ask:
                mids.append((bid + ask) / 2)
    if not mids:
        raise ValueError('no quotes')
    return sorted(mids)[len(mids) // 2]  # median mid across profiles

def goldapi(sym):
    price = _get_json(f'https://api.gold-api.com/price/{sym}').get('price')
    if not price:
        raise ValueError('no price')
    return float(price)

def spot(sym):
    try:
        p = swissquote(sym)
    except Exception:
        p = goldapi(sym)
    if not (isinstance(p, (int, float)) and math.isfinite(p) and p > 0):
        raise ValueError('bad price')
    return p

try:
    gold, silver = spot('XAU'), spot('XAG')
except Exception:
    sys.exit(1)  # caller falls back to Yahoo futures, labeled as such

prev = {}
try:
    with open(state_path) as f:
        prev = json.load(f)
except Exception:
    pass

def line(label, price, prev_price):
    if isinstance(prev_price, (int, float)) and prev_price > 0:
        pct = (price - prev_price) / prev_price * 100
        arrow = '▲' if pct >= 0 else '▼'
        return f"- {label}: ${price:,.2f} {arrow} {pct:+.2f}%"
    return f"- {label}: ${price:,.2f}"

out = [line('Gold', gold, prev.get('gold')), line('Silver', silver, prev.get('silver'))]
try:
    os.makedirs(state_dir, exist_ok=True)
    with open(state_path, 'w') as f:
        json.dump({'gold': gold, 'silver': silver, 'ts': time.time()}, f)
except Exception:
    pass
print('\n'.join(out))
PY
}

echo "US Indexes:"
fetch '%5EGSPC'
fetch '%5EIXIC'
fetch '%5EDJI'

echo
 echo "International:"
fetch '%5EFTSE'
fetch '%5EN225'

echo
 echo "USD Strength (DXY):"
fetch 'DX-Y.NYB'

echo
 echo "Forex:"
fx

echo
 echo "Commodities:"
# Spot gold/silver from stooq (GC=F/SI=F futures trade at a contango premium
# vs spot; Yahoo's GC=F currently tracks a deferred contract, inflating gold).
# Falls back to Yahoo futures, labeled as such, if stooq is unreachable.
spot_metals || { fetch 'GC%3DF'; fetch 'SI%3DF'; }
fetch 'HG%3DF'       # COMEX front-month copper (standard "spot" reference)

echo
 echo "Rates:"
fetch '%5ETNX'
