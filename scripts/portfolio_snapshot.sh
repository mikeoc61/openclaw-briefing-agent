#!/usr/bin/env bash
# Portfolio snapshot for Morning Intel Briefing
# Watchlist grouped into tiers (conviction, thematic leverage, optionality,
# diversifiers); edit the PORTFOLIO dict below to change tickers or tiers.
# Pulls price + prior close from Yahoo Finance; yahoo_session.prior_close
# anchors the baseline on the session date, so long weekends and the current
# session's own bar (whose close is null until the first print) can't shift the
# comparison day.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import json, urllib.request, time
from yahoo_session import prior_close

PORTFOLIO = {
    "Core (High Conviction)": ["COIN", "CRCL", "HOOD", "NVDA"],
    "Secondary (Thematic Leverage)": ["ANET", "PLTR", "GRID"],
    "Optionality": ["XYZ"],
    "Diversifiers (Healthcare/Insurance)": ["LLY", "BBH", "KBWP"],
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
        timestamps = result.get('timestamp', [])

        price = meta.get('regularMarketPrice')
        prev = prior_close(meta, timestamps, closes) or meta.get('previousClose')

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
