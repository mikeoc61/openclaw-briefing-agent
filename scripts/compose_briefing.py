#!/usr/bin/env python3
"""Compose the Morning Intel Brief from collector outputs.

Usage: compose_briefing.py <tmpdir>
Reads fixed-name .txt files produced by briefing_parent.sh collectors
from <tmpdir> and prints the plain-text brief to stdout.
"""
import os, re, sys, json, pathlib, datetime, zoneinfo, urllib.request

try:
    from market_warehouse import build_payload, write_snapshot
except Exception:
    build_payload = write_snapshot = None

TMP = pathlib.Path(sys.argv[1])

def slot(name):
    """Read a collector output file; missing/unreadable → empty string."""
    try:
        return (TMP / f"{name}.txt").read_text().strip()
    except Exception:
        return ""

weather      = slot("weather")
sunrise      = slot("sunrise")
tides        = slot("tides")
power        = slot("powerwall")
econet       = slot("econet")
blink        = slot("blink")
fail2ban_raw = slot("fail2ban")
disk_raw     = slot("disk")
health       = slot("health")
gate         = slot("gateway")
btcnode      = slot("bitcoin_node")
snap         = slot("bitcoin_snapshot")
markets      = slot("markets")
news         = slot("news")
portfolio    = slot("portfolio")
strc         = slot("strc")
calendar_raw = slot("calendar")
btc_sma      = slot("btc_sma")
gw_details   = slot("gw_details")

def parse_json_lines(text):
    """Parse multiple JSON objects from text."""
    results = []
    for line in text.split('\n'):
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return results

def extract_first(pattern, text, default=""):
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else default

# Extract key metrics
weather_lines = [line.strip() for line in weather.splitlines() if line.strip()]
weather_summary = weather_lines  # list of lines, rendered individually below
powerwall_summary = power.strip() if power else "Unavailable"
econet_summary = econet.strip() if econet else "Unavailable"

# System health
cpu_temp = extract_first(r'temp=([0-9.]+)', health, "")
if cpu_temp:
    try:
        c = float(cpu_temp)
        f = (c * 9/5) + 32
        cpu_temp = f"{f:.1f}F ({c}C)"
    except:
        pass
disk_match = re.search(r'/dev/\S+\s+\S+\s+\S+\s+(\S+)\s+(\d+)%', health)
uptime_line = extract_first(r'up\s+(.+?),\s+\d+\s+users', health)


def _to_bytes(s):
    """Parse free(1) values: plain bytes ('8189595648') or humanized ('7.6Gi', '918Mi', '0B')."""
    m = re.match(r'^([0-9.]+)\s*([KMGTP]?)', s.strip(), re.I)
    if not m:
        return None
    mult = {'': 1, 'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4, 'P': 1024**5}
    return float(m.group(1)) * mult[m.group(2).upper()]


def _fmt_gb(b):
    return f"{b / 1024**3:.1f}G"


# Memory pressure: 'available' (col 7 of free) is the kernel's estimate of
# memory allocatable without swapping — reclaimable cache counts as free.
# Effective usage = total - available. Total alone says nothing about health.
mem_line = ""
_mem = re.search(r'Mem:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)', health)
if _mem:
    _total, _avail = _to_bytes(_mem.group(1)), _to_bytes(_mem.group(6))
    if _total and _avail is not None:
        _eff_used = _total - _avail
        _pct = _eff_used / _total * 100
        mem_line = f"{_fmt_gb(_avail)} avail / {_fmt_gb(_total)} ({_pct:.0f}% used)"
        if _pct >= 85:
            mem_line += " ⚠ high pressure"
_swap = re.search(r'Swap:\s+(\S+)\s+(\S+)', health)
if _swap and mem_line:
    _swap_used = _to_bytes(_swap.group(2))
    if _swap_used and _swap_used > 64 * 1024**2:  # ignore trivial residue
        mem_line += f", swap {_fmt_gb(_swap_used)} in use ⚠"

disk_free = disk_used = ""
if disk_match:
    disk_free = disk_match.group(1)
    disk_used = disk_match.group(2)

health_summary = f"CPU: {cpu_temp if cpu_temp else 'unavailable'}"
if disk_used:
    health_summary += f" | Disk used: {disk_used}%"
    if disk_free:
        health_summary += f" (free {disk_free})"
if mem_line:
    health_summary += f" | Memory: {mem_line}"
if uptime_line:
    health_summary += f" | Up: {uptime_line}"

# Gateway
gateway_summary = "Running normally" if gate else "Unavailable"

# Gateway version + LLM details
gw_ver_current = ""
gw_ver_latest = ""
gw_ver_status = ""
gw_llm_provider = ""
gw_llm_model = ""
gw_briefing_model = ""
gw_briefing_fallback = ""
for line in gw_details.split('\n'):
    if line.startswith('version_current='):
        gw_ver_current = line.split('=', 1)[1]
    elif line.startswith('version_latest='):
        gw_ver_latest = line.split('=', 1)[1]
    elif line.startswith('version_status='):
        gw_ver_status = line.split('=', 1)[1]
    elif line.startswith('llm_provider='):
        gw_llm_provider = line.split('=', 1)[1]
    elif line.startswith('llm_model='):
        gw_llm_model = line.split('=', 1)[1]
    elif line.startswith('briefing_model='):
        gw_briefing_model = line.split('=', 1)[1]
    elif line.startswith('briefing_fallback='):
        gw_briefing_fallback = line.split('=', 1)[1]

# Bitcoin node
btcnode_objs = parse_json_lines(btcnode)
btc_blocks = btc_peers = btc_mempool = btc_reachable = btc_external = ""
for obj in btcnode_objs:
    if isinstance(obj, dict):
        if 'blocks' in obj:
            blocks = obj.get('blocks', 0)
            headers = obj.get('headers', 0)
            sync_pct = obj.get('verificationprogress', 1.0)
            btc_blocks = f"Blocks: {blocks}/{headers} ({sync_pct*100:.1f}%)"
        if 'connections' in obj:
            total = obj.get('connections', 0)
            conn_in  = obj.get('connections_in', None)
            conn_out = obj.get('connections_out', None)
            if conn_in is not None and conn_out is not None:
                btc_peers = f"Peers: {total} ({conn_in} in / {conn_out} out)"
                if conn_in > 0:
                    btc_reachable = f"Peers: {conn_in} inbound \u2713"
                else:
                    btc_reachable = "Inbound: 0 (may be firewalled)"
            else:
                btc_peers = f"Peers: {total}"
                btc_reachable = ""
        if 'public_ip' in obj:
            pip = obj.get('public_ip') or 'unknown'
            reachable = obj.get('port_reachable')
            if reachable is True:
                btc_external = f"Public: {pip} port 8333 open \u2713"
            elif reachable is False:
                btc_external = f"Public: {pip} port 8333 UNREACHABLE \u26a0"
            else:
                btc_external = "Public: IP probe failed"
        if 'size' in obj:
            mempool_size = obj.get('size', 0)
            btc_mempool = f"Mempool: {mempool_size} tx"

btcnode_summary = " | ".join([x for x in [btc_blocks, btc_peers, btc_mempool, btc_reachable, btc_external] if x])
if not btcnode_summary:
    info = {}
    for obj in btcnode_objs:
        if isinstance(obj, dict):
            info.update(obj)
    if info:
        parts = []
        if 'blocks' in info and 'headers' in info:
            parts.append(f"Blocks: {info['blocks']}/{info['headers']} ({info.get('verificationprogress', 1.0)*100:.1f}%)")
        if 'connections' in info:
            total = info['connections']
            ci = info.get('connections_in')
            co = info.get('connections_out')
            if ci is not None and co is not None:
                parts.append(f"Peers: {total} ({ci} in / {co} out)")
            else:
                parts.append(f"Peers: {total}")
        if 'size' in info:
            parts.append(f"Mempool: {info['size']} tx")
        btcnode_summary = " | ".join(parts)

# Bitcoin price & snapshot
btc_price_val = extract_first(r'price=([0-9.]+)', btc_sma)
hash_rate = extract_first(r'Hash rate:\s+([^\n]+)', snap)
difficulty = extract_first(r'Difficulty:\s+([^\n]+)', snap)

# Enhanced on-chain metrics from bitcoin_snapshot.sh
retarget_proj = extract_first(r'Retarget:\s+[^\|]+\|\s*proj\s+([+-][0-9.]+)%', snap)
fee_subsidy = extract_first(r'Fee/subsidy 24h:\s*([0-9.]+)%', snap)
blocks_24h = extract_first(r'Blocks 24h:\s*([0-9]+)', snap)
block_fullness = extract_first(r'fullness\s+([0-9]+)%', snap)
p50_fee = extract_first(r'paid p50\s+([0-9.]+)\s+sat/vB', snap)
miner_rev = extract_first(r'miner rev\s+([0-9,.]+)\s+BTC', snap)
tx_rate_7d = extract_first(r'Tx rate \(28d\):\s*([^\n]+)', snap)

# Parse numeric values for Analyst's Take context
retarget_proj_num = None
if retarget_proj:
    try: retarget_proj_num = float(retarget_proj)
    except: pass
fee_subsidy_num = None
if fee_subsidy:
    try: fee_subsidy_num = float(fee_subsidy)
    except: pass
tx_rate_num = None
tx_rate_pct = None
if tx_rate_7d:
    m = re.search(r'([0-9.]+)\s+tx/s\s*.*?([+-][0-9.]+)%', tx_rate_7d)
    if m:
        try: tx_rate_num = float(m.group(1))
        except: pass
        try: tx_rate_pct = float(m.group(2))
        except: pass

bitcoin_summary = ""
if btc_price_val:
    bitcoin_summary = f"Price: ${btc_price_val}"
if hash_rate:
    bitcoin_summary += f" | {hash_rate}"
if difficulty:
    bitcoin_summary += f" | {difficulty}"

# Compact on-chain metrics line (displayed below price/hash/diff)
onchain_line = ""
onchain_parts = []
if blocks_24h:
    onchain_parts.append(f"{blocks_24h} blks/24h")
if block_fullness:
    onchain_parts.append(f"{block_fullness}% full")
if p50_fee:
    onchain_parts.append(f"p50 {p50_fee} sat/vB")
if fee_subsidy:
    onchain_parts.append(f"fee/subsidy {fee_subsidy}%")
if miner_rev:
    onchain_parts.append(f"miner rev {miner_rev} BTC")
if tx_rate_7d:
    onchain_parts.append(tx_rate_7d)
if retarget_proj:
    onchain_parts.append(f"retarget {retarget_proj}%")
if onchain_parts:
    onchain_line = "On-chain: " + " | ".join(onchain_parts)

# Global markets
market_lines = [line.strip() for line in markets.split('\n') if line.strip()] if markets else []

# Crypto news
news_lines = [x.strip() for x in news.split('\n') if x.strip() and x.strip().startswith('-')][:5]

# STRC (Strategy Inc.) — digital credit proxy
strc_display = []
strc_price_num = None
strc_pct_val = None
strc_vol_ratio = None
strc_hi52 = None
strc_lo52 = None
for line in strc.split('\n'):
    if line.startswith('strc_price='):
        try: strc_price_num = float(line.split('=', 1)[1])
        except: pass
    elif line.startswith('strc_pct='):
        try: strc_pct_val = float(line.split('=', 1)[1])
        except: pass
    elif line.startswith('strc_vol_ratio='):
        try: strc_vol_ratio = float(line.split('=', 1)[1])
        except: pass
    elif line.startswith('strc_hi52='):
        try: strc_hi52 = float(line.split('=', 1)[1])
        except: pass
    elif line.startswith('strc_lo52='):
        try: strc_lo52 = float(line.split('=', 1)[1])
        except: pass
    elif not line.startswith('strc_') and line.strip():
        strc_display.append(line)
strc_summary = '\n'.join(strc_display) if strc_display else 'Unavailable'

# Compose brief
tz = zoneinfo.ZoneInfo('Pacific/Honolulu')
now = datetime.datetime.now(tz)
date_str = now.strftime('%A, %B %-d, %Y %-I:%M %p')

lines = []
lines.append(f"MORNING INTEL BRIEF — {date_str} HST")
lines.append("")
lines.append(f"WEATHER — {os.environ.get('BRIEFING_WEATHER_LABEL', 'HOME')}")
for wl in (weather_summary if isinstance(weather_summary, list) else [weather_summary]):
    lines.append(wl)
lines.append("")
lines.append("SUNRISE / SUNSET / TIDES")
lines.append(sunrise if sunrise else "unavailable")
lines.append(tides if tides else "unavailable")
lines.append("")
lines.append("TODAY'S CALENDAR")
cal_lines = []
try:
    cal_data = json.loads(calendar_raw) if calendar_raw else {}
    cal_error = cal_data.get("error")
    if cal_error == "auth_failed":
        cal_lines.append("Calendar: authentication failed — regenerate app-specific password")
    elif cal_error:
        cal_lines.append(f"Calendar: unavailable ({cal_error})")
    elif not cal_data.get("events"):
        cal_lines.append("No events scheduled for today.")
    else:
        events = cal_data["events"]
        calendars_present = {e.get("calendar") for e in events if e.get("calendar")}
        show_suffix = len(calendars_present) > 1
        for ev in events:
            cal_tag = f" [{ev['calendar']}]" if show_suffix and ev.get("calendar") else ""
            if ev.get("all_day"):
                cal_lines.append(f"- All day — {ev['title']}{cal_tag}")
            else:
                loc = f" @ {ev['location']}" if ev.get("location") else ""
                cal_lines.append(f"- {ev['start']} — {ev['title']}{loc}{cal_tag}")
except Exception as e:
    cal_lines.append(f"Calendar: unavailable ({e})")
for cl in cal_lines:
    lines.append(cl)
lines.append("")
lines.append("POWERWALL / HOME ENERGY")
lines.append(powerwall_summary)
lines.append("")
lines.append("HEAT PUMP WATER HEATER")
lines.append(econet_summary)
lines.append("")
lines.append("SYSTEM HEALTH")
if health_summary:
    lines.append(health_summary)
else:
    lines.append("Unavailable")
lines.append("")
lines.append("DISK HEALTH")
def format_disk_smart(raw):
    try:
        data = json.loads(raw)
    except Exception:
        return "unavailable (parse error)"
    if data.get('error'):
        return f"unavailable ({data['error']})"
    disks = data.get('disks', [])
    smart_disks = [d for d in disks if d.get('info', {}).get('smart_capable', True)
                   and not d.get('info', {}).get('device', '').startswith('/dev/mmcblk')]
    if not smart_disks:
        return "no SMART-capable disks detected"
    parts = []
    for d in smart_disks:
        info    = d.get('info', {})
        verdict = d.get('verdict', {})
        summ    = d.get('summary', {})
        model   = info.get('model') or info.get('device', '?')
        v       = verdict.get('verdict', 'unknown')
        reasons = verdict.get('reasons', [])
        if v == 'OK' and summ.get('kind'):
            attrs = []
            status = summ.get('smart_status', 'PASSED')
            if summ.get('percentage_used') is not None:
                attrs.append(f"endurance {summ['percentage_used']}% used")
            if summ.get('available_spare') is not None:
                attrs.append(f"spare {summ['available_spare']}%")
            if summ.get('media_errors'):
                attrs.append(f"\u26a0 media_errors={summ['media_errors']}")
            if summ.get('temperature_c') is not None:
                attrs.append(f"{summ['temperature_c']}\u00b0C")
            poh = summ.get('power_on_hours')
            if poh is not None:
                if isinstance(poh, dict):
                    raw_val = poh.get('raw_value', 0)
                    attrs.append(f"{raw_val:,} hrs on")
                else:
                    attrs.append(f"{poh:,} hrs on")
            line = f"{model}: {status}"
            if attrs:
                line += " | " + " | ".join(attrs)
            parts.append(line)
        elif v == 'CRITICAL':
            reason_str = "; ".join(reasons[:2]) if reasons else v
            parts.append(f"\u26a0\u26a0 {model}: CRITICAL \u2014 {reason_str}")
        elif v == 'WARN':
            reason_str = "; ".join(reasons[:2]) if reasons else v
            parts.append(f"\u26a0 {model}: WARN \u2014 {reason_str}")
        else:
            reason_str = reasons[0] if reasons else 'unknown'
            parts.append(f"{model}: {reason_str}")
    return "\n".join(parts)
disk_summary = format_disk_smart(disk_raw)
for dl in disk_summary.splitlines():
    lines.append(dl)
lines.append("")
lines.append("BLINK CAMERAS")
if blink:
    lines.append(blink)
else:
    lines.append("No issues detected.")
lines.append("")
lines.append("SSH SECURITY")
try:
    fb = json.loads(fail2ban_raw) if fail2ban_raw else {}
    fb_err = fb.get('error')
    if fb_err:
        lines.append(f"fail2ban: unavailable ({fb_err})")
    else:
        banned_now   = fb.get('current', {}).get('banned_now', 0)
        bans_24h     = fb.get('activity_24h', {}).get('bans', 0)
        top_offenders = fb.get('activity_24h', {}).get('top_offenders', '') or ''
        delta_bans   = fb.get('delta_since_last_run', {}).get('new_bans', 0)
        delta_fails  = fb.get('delta_since_last_run', {}).get('new_failures', 0)
        jail         = fb.get('jail', 'sshd')
        # Compose signal-sensitive summary
        fb_parts = []
        fb_parts.append(f"Jail: {jail}")
        fb_parts.append(f"Banned now: {banned_now}")
        fb_parts.append(f"24h bans: {bans_24h}")
        fb_parts.append(f"Since last run: +{delta_bans} bans / +{delta_fails} failures")
        lines.append(" | ".join(fb_parts))
        # Elevated activity warnings
        if bans_24h > 100:
            lines.append(f"⚠ HIGH ban rate in last 24h ({bans_24h}) — possible coordinated scan or brute-force campaign.")
            if top_offenders and top_offenders != 'none':
                lines.append(f"Top offenders: {top_offenders}")
        elif bans_24h > 20:
            lines.append(f"Note: elevated ban activity ({bans_24h} in 24h).")
            if top_offenders and top_offenders != 'none':
                lines.append(f"Top offenders: {top_offenders}")
        elif top_offenders and top_offenders != 'none' and bans_24h > 0:
            lines.append(f"Top offenders: {top_offenders}")
except Exception as e:
    lines.append(f"fail2ban: parse error ({e})")
lines.append("")
lines.append("OPENCLAW GATEWAY")
lines.append(gateway_summary)
if gw_ver_current:
    ver_line = f"Version: {gw_ver_current}"
    if gw_ver_status == "up-to-date":
        ver_line += " \u2713"
    elif gw_ver_status and gw_ver_status != "up-to-date":
        ver_line += f" \u26a0 {gw_ver_status}"
    lines.append(ver_line)
if gw_llm_provider and gw_llm_model:
    lines.append(f"Default LLM: {gw_llm_provider}/{gw_llm_model}")
if gw_briefing_model:
    brief_llm = f"Briefing LLM: {gw_briefing_model}"
    if gw_briefing_fallback:
        brief_llm += f" (fallback: {gw_briefing_fallback})"
    lines.append(brief_llm)
lines.append("")
lines.append("BITCOIND NODE")
lines.append(btcnode_summary if btcnode_summary else "Unavailable")
lines.append("")
lines.append("BITCOIN")
#lines.append(bitcoin_summary if bitcoin_summary else "Unavailable")
lines.append(bitcoin_summary if bitcoin_summary else "Unavailable")
if onchain_line:
    lines.append(onchain_line)

try:
    _flows = json.loads((pathlib.Path.home() / '.openclaw/cache/farside_btc.json').read_text())
    etf_flows_line = _flows.get('line')
    etf_flows_summary = _flows.get('summary', {})
except Exception:
    etf_flows_line = None
    etf_flows_summary = {}

if etf_flows_line:
    lines.append(etf_flows_line)
# btc_sma contains a 'price=...' line for extraction + the display line; show only the SMA line
btc_sma_display = '\n'.join(l for l in btc_sma.splitlines() if not l.startswith('price='))
if btc_sma_display:
    lines.append(btc_sma_display)
lines.append("")
lines.append("GLOBAL MARKETS")
if market_lines:
    for line in market_lines:
        lines.append(line)
else:
    lines.append("Unavailable")
lines.append("")
lines.append("CRYPTO HEADLINES")
for line in news_lines:
    lines.append(line)
lines.append("")
lines.append("PORTFOLIO — AI/CRYPTO CONVERGENCE")
if portfolio:
    for line in portfolio.splitlines():
        lines.append(line)
else:
    lines.append("Unavailable")
lines.append("")
lines.append("DIGITAL CREDIT — STRC (Strategy Inc.)")
lines.append(strc_summary)
lines.append("")
lines.append("ANALYST'S TAKE")

# ── Market-session awareness ─────────────────────────────────────────────────
# briefing_parent.sh writes market_status.txt via market_calendar.py:
#   open | closed:weekend | closed:holiday:<name>
# When closed, US equity collectors were skipped upstream and their sections
# carry a "closed" line instead of stale quotes. Bitcoin trades 24/7, so BTC
# price and hash-rate are always live regardless.
market_status = slot("market_status")
if not market_status:  # fallback if the status file is missing
    market_status = "closed:weekend" if now.weekday() >= 5 else "open"
if market_status == "closed:weekend":
    session_desc = "weekend (US equity markets closed — no US session data collected)"
elif market_status.startswith("closed:holiday:"):
    _holiday = market_status.split(":", 2)[2]
    session_desc = (f"US market holiday — {_holiday} (US equities closed; "
                    "intl equities, FX, and metals data are live)")
else:
    session_desc = now.strftime("%A")

# ── Market data parsers ──────────────────────────────────────────────────────

def market_pct(label):
    m = re.search(rf"{re.escape(label)}:.*?([▲▼])\s*([+-][0-9.]+)%", markets)
    if not m:
        return None, None
    return m.group(1), float(m.group(2))

def fx_rate(pair):
    """Return the displayed rate for a forex pair label (e.g. 'EUR/USD')."""
    m = re.search(rf"{re.escape(pair)}:\s*([0-9.]+)", markets)
    if not m:
        return None
    try:
        return float(m.group(1))
    except:
        return None

_, spx_pct    = market_pct("S&P 500")
_, ndq_pct    = market_pct("Nasdaq")
_, dow_pct    = market_pct("Dow Jones")
_, ftse_pct   = market_pct("FTSE 100")
_, nikkei_pct = market_pct("Nikkei")
_, dxy_pct    = market_pct("DXY")
_, gold_pct   = market_pct("Gold")
_, copper_pct = market_pct("Copper")
# 10yr yield: output is "X.XXX% ▲/▼ ±Y.Ybps" — parse bps directly
def yr10_bps():
    m = re.search(r"US 10yr yield:\s*([0-9.]+)%\s*([\u25b2\u25bc])\s*([+-][0-9.]+)bps", markets)
    if not m:
        return None, None, None
    level = float(m.group(1))
    arrow = m.group(2)
    bps   = float(m.group(3))
    return level, arrow, bps

yr10_level, _, yr10_bps_val = yr10_bps()

# FX: EUR/USD complements DXY; USD/JPY is the carry/liquidity tell
eurusd = fx_rate("EUR/USD")
usdjpy = fx_rate("USD/JPY")

# ── Derived aggregates ───────────────────────────────────────────────────────

us_moves   = [x for x in [spx_pct, ndq_pct, dow_pct] if x is not None]
intl_moves  = [x for x in [ftse_pct, nikkei_pct] if x is not None]

def avg(vals):
    return sum(vals) / len(vals) if vals else None

us_avg   = avg(us_moves)
intl_avg = avg(intl_moves)

btc_price_num = None
if btc_price_val:
    try:
        btc_price_num = float(btc_price_val.replace(',', ''))
    except:
        pass

# Parse 200-day SMA from btc_sma string
btc_sma_num = None
btc_sma_pct = None
if btc_sma and 'unavailable' not in btc_sma:
    sma_m = re.search(r'200d SMA:\s*\$([0-9,]+)', btc_sma)
    pct_m = re.search(r'([+-][0-9.]+)%', btc_sma)
    if sma_m:
        try:
            btc_sma_num = float(sma_m.group(1).replace(',', ''))
        except:
            pass
    if pct_m:
        try:
            btc_sma_pct = float(pct_m.group(1))
        except:
            pass

# ── LLM-based analyst take ──────────────────────────────────────────────────

def llm_analyst_take():

    # Read the agent's configured model from openclaw.json — no hardcoded models
    config_path = pathlib.Path.home() / '.openclaw/openclaw.json'
    try:
        config = json.loads(config_path.read_text())
        agent_list = config.get('agents', {}).get('list', [])
        brief_cfg = next((a for a in agent_list if a.get('id') == 'briefing'), {})
        model_ref = (brief_cfg.get('model') or {}).get('primary', '')
        if '/' not in model_ref:
            return None
        provider, model_id = model_ref.split('/', 1)
    except Exception:
        return None

    # Map provider → (API base, env var, is_anthropic_format)
    PROVIDERS = {
        'deepseek':  ('https://api.deepseek.com',     'DEEPSEEK_API_KEY',  False),
        'openai':    ('https://api.openai.com',        'OPENAI_API_KEY',    False),
        'anthropic': ('https://api.anthropic.com',     'ANTHROPIC_API_KEY', True),
    }
    entry = PROVIDERS.get(provider)
    if not entry:
        return None
    base_url, env_key, is_anthropic = entry

    # Read API key: try environment first, fall back to openclaw.env
    # (cron-isolated sessions don't source .bashrc, so env vars won't be set)
    api_key = os.environ.get(env_key, '')
    if not api_key:
        try:
            env_path = pathlib.Path.home() / '.openclaw' / 'openclaw.env'
            for line in env_path.read_text().splitlines():
                if line.startswith(env_key + '='):
                    api_key = line.split('=', 1)[1].strip()
                    break
        except Exception:
            pass
    if not api_key:
        return None

    ctx_lines = [f"Session: {session_desc}"]
    if btc_price_num is not None:
        ctx_lines.append(f"BTC price: ${btc_price_num:,.0f}")
    if btc_sma_num is not None and btc_sma_pct is not None:
        ctx_lines.append(f"BTC 200d SMA: ${btc_sma_num:,.0f} | price vs SMA: {btc_sma_pct:+.1f}%")
    if hash_rate:
        ctx_lines.append(f"BTC hash rate: {hash_rate}")
    if retarget_proj_num is not None:
        ctx_lines.append(f"BTC retarget projection: {retarget_proj_num:+.2f}% — miner pressure signal")
    if fee_subsidy_num is not None:
        ctx_lines.append(f"BTC fee/subsidy 24h: {fee_subsidy_num:.2f}% — under 1%% = apathy floor, over 3%% = demand return")
    if blocks_24h and block_fullness and p50_fee:
        ctx_lines.append(f"BTC blocks 24h: {blocks_24h}, {block_fullness}%% full, p50 paid fee {p50_fee} sat/vB (full+low=filler, not demand)")
    if miner_rev:
        ctx_lines.append(f"BTC miner revenue 24h: {miner_rev} BTC")
    if tx_rate_num is not None and tx_rate_pct is not None:
        ctx_lines.append(f"BTC tx rate (28d): {tx_rate_num:.2f} tx/s ({tx_rate_pct:+.1f}%% 7d)")
    if etf_flows_line:
        ctx_lines.append(etf_flows_line)
        # Include day-of-week for the latest data point to prevent LLM hallucination
        as_of = etf_flows_summary.get('as_of')
        if as_of:
            try:
                dt = datetime.datetime.strptime(as_of, "%d %b %Y")
                ctx_lines.append(f"ETF data as-of day-of-week: {dt.strftime('%A')}")
            except ValueError:
                pass
        _div = (
            etf_flows_summary.get('window_lead') is not None
            and etf_flows_summary['window_lead'] > 0
            and btc_price_num is not None
            and btc_sma_num is not None
            and btc_price_num < btc_sma_num
        )
        if _div:
            ctx_lines.append(
                "BTC ETF flow/price divergence: IBIT net inflow with price below "
                "200d SMA — possible demand return"
            )
    if btcnode_summary:
        ctx_lines.append(f"BTC node: {btcnode_summary}")
    if us_avg is not None:
        eq_parts = [f"avg {us_avg:+.1f}%"]
        if spx_pct is not None: eq_parts.append(f"SPX {spx_pct:+.1f}%")
        if ndq_pct is not None: eq_parts.append(f"NDQ {ndq_pct:+.1f}%")
        if dow_pct is not None: eq_parts.append(f"Dow {dow_pct:+.1f}%")
        ctx_lines.append("US equities: " + " | ".join(eq_parts))
    if intl_avg is not None:
        intl_parts = [f"avg {intl_avg:+.1f}%"]
        if ftse_pct is not None: intl_parts.append(f"FTSE {ftse_pct:+.1f}%")
        if nikkei_pct is not None: intl_parts.append(f"Nikkei {nikkei_pct:+.1f}%")
        ctx_lines.append("Intl equities (fresher than US close at 6am HST): " + " | ".join(intl_parts))
    if dxy_pct is not None:
        ctx_lines.append(f"DXY: {dxy_pct:+.1f}%")
    fx_parts = []
    if eurusd is not None: fx_parts.append(f"EUR/USD {eurusd:.4f}")
    if usdjpy is not None: fx_parts.append(f"USD/JPY {usdjpy:.2f}")
    if fx_parts:
        ctx_lines.append("FX: " + " | ".join(fx_parts))
    if yr10_level is not None and yr10_bps_val is not None:
        ctx_lines.append(f"US 10yr yield: {yr10_level:.3f}% ({yr10_bps_val:+.1f}bps)")
    if gold_pct is not None:
        ctx_lines.append(f"Gold: {gold_pct:+.1f}%")
    if copper_pct is not None:
        ctx_lines.append(f"Copper: {copper_pct:+.1f}%")
    if strc_price_num is not None and strc_pct_val is not None:
        ctx_lines.append(f"STRC (Strategy Inc.): ${strc_price_num:.2f} ({strc_pct_val:+.2f}%)")
        if strc_vol_ratio is not None:
            ctx_lines.append(f"STRC volume: {strc_vol_ratio:.1f}x avg")
            # Normalize for partial trading day — at 6am HST (noon ET) the NASDAQ
            # is ~38% through its session; 0.4x is perfectly normal, not low.
            et_now = datetime.datetime.now(zoneinfo.ZoneInfo('America/New_York'))
            mkt_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
            mkt_close = et_now.replace(hour=16, minute=0, second=0, microsecond=0)
            session_min = (mkt_close - mkt_open).total_seconds() / 60.0
            elapsed_min = max(0, min(session_min, (et_now - mkt_open).total_seconds() / 60.0))
            if 0 < elapsed_min < session_min:
                pct = elapsed_min / session_min * 100
                expected_mult = elapsed_min / session_min
                effective_ratio = strc_vol_ratio / expected_mult if expected_mult > 0 else strc_vol_ratio
                ctx_lines.append(
                    f"STRC volume context: market ~{pct:.0f}% through session; "
                    f"pro-rata expected {expected_mult:.2f}x full-day avg → "
                    f"effective pace: {effective_ratio:.1f}x (only flag if materially outside 0.7–1.3x range)"
                )
        if strc_hi52 and strc_lo52 and strc_price_num:
            range_pct = ((strc_price_num - strc_lo52) / (strc_hi52 - strc_lo52)) * 100
            ctx_lines.append(f"STRC in 52w range: {range_pct:.0f}% from low (${strc_lo52:.0f}–${strc_hi52:.0f})")
    news_titles = [
        l.lstrip('- ').split(' — ')[0]
        for l in news.splitlines()
        if l.startswith('- ') and 'unavailable' not in l.lower()
    ]
    if news_titles:
        ctx_lines.append("Crypto headlines: " + " | ".join(news_titles[:4]))

    context = "\n".join(ctx_lines)
    prompt = (
        "You are Kai, the analyst behind a daily morning intelligence brief for a "
        "Bitcoin-focused long-horizon investor in Hawaii. Write the ANALYST'S TAKE "
        "section — a focused analytical paragraph, up to 200 words. Rules: signal over "
        "noise only; if nothing is notable just say so plainly; no filler, no hedging "
        "for its own sake; connect the dots between macro, BTC price, network "
        "health, and digital credit markets (STRC) where genuinely relevant; "
        "STRC is Strategy Inc. (Saylor's Bitcoin treasury) — its price and volume "
        "signal institutional BTC demand through convertible debt markets; "
        "elevated STRC volume often precedes BTC spot moves. "
	"ETF flows are the cleanest read on institutional spot demand: "
	"sustained IBIT-led outflows are conviction distribution; "
	"a flip to IBIT-led inflows against a sub-200d price is an early bottoming tell. "
	"Weight IBIT over the headline total. "
        "Be direct and specific. "
        "Do not use headers or bullet points. Output only the paragraph text.\n\n"
        f"Market data:\n{context}"
    )

    if is_anthropic:
        payload = json.dumps({
            "model": model_id,
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            f'{base_url}/v1/messages',
            data=payload,
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            return resp['content'][0]['text'].strip()
        except Exception:
            return None
    else:
        # OpenAI-compatible (DeepSeek, OpenAI, etc.)
        # Reasoning models (e.g. deepseek-v4-pro) consume tokens for internal
        # reasoning; we need a larger budget than the 200-word output target.
        payload = json.dumps({
            "model": model_id,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            f'{base_url}/v1/chat/completions',
            data=payload,
            headers={
                'Authorization': f'Bearer {api_key}',
                'content-type': 'application/json',
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            content = resp['choices'][0]['message'].get('content', '').strip()
            # Reasoning models may exhaust max_tokens on reasoning,
            # leaving content empty. Fall back to reasoning_content if so.
            if not content:
                rc = resp['choices'][0]['message'].get('reasoning_content', '').strip()
                if rc:
                    # Take the last paragraph of reasoning as analysis
                    paras = rc.split('\n\n')
                    content = paras[-1] if paras else rc
            return content
        except Exception:
            return None

analyst_take = llm_analyst_take()
lines.append(analyst_take if analyst_take else "Analysis unavailable.")
print("\n".join(lines))

if write_snapshot is not None:
    try:
        write_snapshot(
            now.strftime("%Y-%m-%d"),
            build_payload(
                hash_rate=hash_rate,
                difficulty=difficulty,
                retarget_proj_num=retarget_proj_num,
                fee_subsidy_num=fee_subsidy_num,
                blocks_24h=blocks_24h,
                block_fullness=block_fullness,
                p50_fee=p50_fee,
                miner_rev=miner_rev,
                tx_rate_num=tx_rate_num,
                tx_rate_pct=tx_rate_pct,
                btc_price_num=btc_price_num,
                btc_sma_num=btc_sma_num,
                btc_sma_pct=btc_sma_pct,
            ),
        )
    except Exception:
        pass
