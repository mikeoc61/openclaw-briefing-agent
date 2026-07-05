#!/usr/bin/env bash
# Since this node runs a full blockchain archival node, calculate hashrate and difficulty 
# 
set -euo pipefail
BCLI="bitcoin-cli"
WINDOW=1008

# Fail soft: silence is ambiguous with "collector died" — always emit a line.
trap 'echo "=== BITCOIN NETWORK SNAPSHOT ==="; echo "Hash rate/difficulty: unavailable (bitcoin-cli error)"; exit 0' ERR

tip=$($BCLI getblockcount)
hr_now=$($BCLI getnetworkhashps "$WINDOW")
hr_old=$($BCLI getnetworkhashps "$WINDOW" $((tip - WINDOW)))
diff_now=$($BCLI getdifficulty)
old_hash=$($BCLI getblockhash $((tip - WINDOW)))
diff_old=$($BCLI getblockheader "$old_hash" | python3 -c 'import sys,json; print(json.load(sys.stdin)["difficulty"])')

python3 - "$hr_now" "$hr_old" "$diff_now" "$diff_old" <<'PY'
import sys
hn, ho, dn, do = map(float, sys.argv[1:5])
def line(label, now, old, scale, unit):
    pct = (now - old) / old * 100 if old else 0
    arrow = '\u25b2' if pct >= 0 else '\u25bc'
    print(f'{label}: {now/scale:,.2f} {unit} {arrow} {pct:+.2f}% (7d)')
print('=== BITCOIN NETWORK SNAPSHOT ===')
line('Hash rate', hn, ho, 1e18, 'EH/s')
line('Difficulty', dn, do, 1e12, 'T')
PY
