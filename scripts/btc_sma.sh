#!/usr/bin/env bash
# btc_sma.sh — 200-day SMA check for BTC
# Fetches 200 daily closes from CoinGecko (Binance fallback),
# computes the 200-day SMA, and reports where price sits relative to it.
# Output: single structured line for briefing_parent.sh to consume.

set -euo pipefail

python3 - <<'PY'
import json, urllib.request, sys

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'

def fetch_coingecko():
    url = (
        'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart'
        '?vs_currency=usd&days=201&interval=daily'
    )
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    prices = data.get('prices', [])
    # Each entry: [timestamp_ms, price]
    closes = [p[1] for p in prices if isinstance(p[1], (int, float))]
    return closes

def fetch_binance():
    url = (
        'https://api.binance.com/api/v3/klines'
        '?symbol=BTCUSDT&interval=1d&limit=202'
    )
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    # kline: [open_time, open, high, low, close, ...]
    closes = [float(k[4]) for k in data]
    return closes

closes = None
source = None
for name, fn in [('CoinGecko', fetch_coingecko), ('Binance', fetch_binance)]:
    try:
        closes = fn()
        source = name
        break
    except Exception as e:
        continue

if not closes or len(closes) < 200:
    print('200d SMA: unavailable')
    sys.exit(0)

# Use last 200 completed daily closes (exclude in-progress candle if present)
# CoinGecko often returns today's partial candle as last entry — drop it
window = closes[-201:-1] if len(closes) >= 201 else closes[-200:]
sma_200 = sum(window) / len(window)

# Current price = most recent close (or last completed)
current = closes[-1]

pct_diff = ((current - sma_200) / sma_200) * 100
arrow = '▲' if pct_diff >= 0 else '▼'

# Classify
if pct_diff > 2.0:
    signal = 'above (support)'
elif pct_diff >= -2.0:
    signal = 'near (key level)'
else:
    signal = 'below (resistance)'

# Price line in log-compatible format for the briefing composer
print(f'price={current:.0f}')
print(
    f'200d SMA: ${sma_200:,.0f} | '
    f'{arrow} {pct_diff:+.1f}% — {signal}'
)
PY
