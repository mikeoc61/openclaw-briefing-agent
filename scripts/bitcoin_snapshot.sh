#!/usr/bin/env bash
# Bitcoin network snapshot for daily briefing: hashrate, difficulty, retarget
# projection, fee environment, mempool depth, 24h block economics, tx rate.
set -euo pipefail
BCLI="bitcoin-cli"
WINDOW=1008
LOOKBACK=160
TXWINDOW=4032
STATFIELDS='["time","totalfee","subsidy","feerate_percentiles","total_weight","txs"]'

# Fail soft: silence is ambiguous with "collector died" — always emit a line.
trap 'echo "=== BITCOIN NETWORK SNAPSHOT ==="; echo "Network data: unavailable (bitcoin-cli error)"; exit 0' ERR

tip=$($BCLI getblockcount)
hr_now=$($BCLI getnetworkhashps "$WINDOW")
hr_old=$($BCLI getnetworkhashps "$WINDOW" $((tip - WINDOW)))
diff_now=$($BCLI getdifficulty)
old_hash=$($BCLI getblockhash $((tip - WINDOW)))
diff_old=$($BCLI getblockheader "$old_hash" | jq .difficulty)

retarget_start=$((tip - tip % 2016))
start_time=$($BCLI getblockheader "$($BCLI getblockhash "$retarget_start")" | jq .time)
tip_time=$($BCLI getblockheader "$($BCLI getblockhash "$tip")" | jq .time)

fee_fast=$($BCLI estimatesmartfee 2)
fee_hour=$($BCLI estimatesmartfee 6)
fee_day=$($BCLI estimatesmartfee 144)
mempool=$($BCLI getmempoolinfo)
tx_now=$($BCLI getchaintxstats "$TXWINDOW")
tx_old=$($BCLI getchaintxstats "$TXWINDOW" "$old_hash")

DAY_STATS=$(for ((h = tip - LOOKBACK + 1; h <= tip; h++)); do
    "$BCLI" getblockstats "$h" "$STATFIELDS"
done | jq -s -c .)
export DAY_STATS

python3 - "$hr_now" "$hr_old" "$diff_now" "$diff_old" \
          "$fee_fast" "$fee_hour" "$fee_day" "$mempool" \
          "$tx_now" "$tx_old" "$tip" "$retarget_start" \
          "$start_time" "$tip_time" <<'PY'
import sys, os, json, time, statistics

hn, ho, dn, do = map(float, sys.argv[1:5])
fee_fast, fee_hour, fee_day, mempool, tx_now, tx_old = (json.loads(a) for a in sys.argv[5:11])
tip, retarget_start, start_time, tip_time = map(int, sys.argv[11:15])
day = json.loads(os.environ["DAY_STATS"])

def line(label, now, old, scale, unit):
    pct = (now - old) / old * 100 if old else 0
    arrow = '\u25b2' if pct >= 0 else '\u25bc'
    print(f'{label}: {now/scale:,.2f} {unit} {arrow} {pct:+.2f}% (7d)')

def satvb(est):
    fr = est.get('feerate')
    if fr is None:
        return 'n/a'
    v = fr * 1e5
    return f'{v:.1f}' if v < 10 else f'{v:.0f}'

print('=== BITCOIN NETWORK SNAPSHOT ===')
line('Hash rate', hn, ho, 1e18, 'EH/s')
line('Difficulty', dn, do, 1e12, 'T')

blocks_elapsed = tip - retarget_start
blocks_left = 2016 - tip % 2016
if blocks_elapsed > 0 and tip_time > start_time:
    pace = (tip_time - start_time) / blocks_elapsed
    proj = (600 / pace - 1) * 100
    eta_d = blocks_left * pace / 86400
    print(f'Retarget: {blocks_left} blks (~{eta_d:.1f}d) | proj {proj:+.2f}%')
else:
    print(f'Retarget: {blocks_left} blks | proj n/a')

print(f'Fees est: {satvb(fee_fast)}/{satvb(fee_hour)}/{satvb(fee_day)} sat/vB (fast/1hr/1d)')
print(f"Mempool: {mempool['size']:,} tx / {mempool['bytes']/1e6:.1f} vMB")

cutoff = time.time() - 86400
d24 = [b for b in day if b['time'] >= cutoff]
if d24:
    fees = sum(b['totalfee'] for b in d24)
    subsidy = sum(b['subsidy'] for b in d24)
    fullness = statistics.mean(b['total_weight'] for b in d24) / 4e6 * 100
    p50 = statistics.median(b['feerate_percentiles'][2] for b in d24)
    txs = sum(b['txs'] for b in d24)
    rev = (fees + subsidy) / 1e8
    print(f'Blocks 24h: {len(d24)} | fullness {fullness:.0f}% | paid p50 {p50:.1f} sat/vB')
    print(f'Fee/subsidy 24h: {fees/subsidy*100:.2f}% | miner rev {rev:,.1f} BTC | {txs:,} txs')
else:
    print('Blocks 24h: no data in lookback window')

trn, tro = tx_now.get('txrate'), tx_old.get('txrate')
if trn and tro:
    pct = (trn - tro) / tro * 100
    arrow = '\u25b2' if pct >= 0 else '\u25bc'
    print(f'Tx rate (28d): {trn:.2f} tx/s {arrow} {pct:+.2f}% (7d)')
elif trn:
    print(f'Tx rate (28d): {trn:.2f} tx/s')
PY
