#!/usr/bin/env bash
# Portfolio snapshot for Morning Intel Briefing
# AI–Crypto Convergence: High-Convexity Monetization Portfolio
# Pulls price + prior close from Yahoo Finance using clean_closes[-2] to
# avoid the stale previousClose artifact that appears after long weekends.

set -euo pipefail

python3 - <<'PY'
import json, urllib.request, math, time

PORTFOLIO = {
    "Core (High Conviction)": ["ANET", "COIN", "CRCL", "PLTR"],
    "Secondary (Thematic Leverage)": ["SOXX", "HUT"],
    "Optionality": ["RKLB"],
}

ALL_TICKERS = [t for tickers in PORTFOLIO.values() for t in tickers]

def fetch(ticker):
    url = f'https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=10d'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        'Accept': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        result = d['chart']['result'][0]
        meta = result['meta']
        closes = result['indicators']['quote'][0].get('close', [])
        clean = [x for x in closes if isinstance(x, (int, float)) and math.isfinite(x)]

        price = meta.get('regularMarketPrice')
        prev = clean[-2] if len(clean) >= 2 else None

        if price is None or prev is None:
            return None

        chg = price - prev
        pct = (chg / prev) * 100
        arrow = '▲' if chg >= 0 else '▼'

        # 52-week range from full 10-day slice is too short; use meta fields
        hi52 = meta.get('fiftyTwoWeekHigh')
        lo52 = meta.get('fiftyTwoWeekLow')
        from_hi = ((price - hi52) / hi52 * 100) if hi52 else None

        return {
            'ticker': ticker,
            'price': price,
            'prev': prev,
            'pct': pct,
            'arrow': arrow,
            'hi52': hi52,
            'lo52': lo52,
            'from_hi': from_hi,
        }
    except Exception as e:
        return {'ticker': ticker, 'error': str(e)}

results = {}
for ticker in ALL_TICKERS:
    results[ticker] = fetch(ticker)
    time.sleep(0.35)

for group, tickers in PORTFOLIO.items():
    print(f"\n{group}:")
    for t in tickers:
        r = results.get(t)
        if not r or 'error' in r:
            print(f"  {t}: unavailable")
            continue
        pct_str = f"{r['pct']:+.2f}%"
        price_str = f"${r['price']:.2f}"
        from_hi_str = f"  (52w hi: ${r['hi52']:.2f}, {r['from_hi']:+.1f}%)" if r['hi52'] and r['from_hi'] is not None else ""
        print(f"  {t}: {price_str}  {r['arrow']} {pct_str}{from_hi_str}")
PY
