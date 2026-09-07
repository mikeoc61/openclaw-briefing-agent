# OpenClaw Morning Briefing Agent

A self-hosted morning intelligence briefing that runs on a Raspberry Pi and
delivers a single, opinionated daily brief via **HTML email** (msmtp) and
**Signal** (OpenClaw messaging). It aggregates weather, tides, markets,
crypto/on-chain data, home energy, and system health — then synthesizes an
analyst's take rather than just dumping data.

Built as a workspace for an OpenClaw agent run each morning by OpenClaw's own
scheduler (not cron or systemd), but the pipeline is plain bash + Python and
runs standalone.

## Design philosophy

- **Signal over noise** — the brief tells you what matters, not just what happened. A composer layer interprets cross-asset moves (e.g., gold up with yields flat → debasement demand, not rate mechanics) instead of listing prices.
- **Fail soft** — every collector degrades to an "unavailable" line; one dead API never kills the brief.
- **Idempotent** — a daily sent-marker plus a `flock` guard make re-runs (cron retries, LLM failover re-delivery) exit cleanly without duplicate sends.
- **No secrets or PII in git** — all tokens, credentials, contact info, and location data live in local-only files outside the repo. See [Configuration](#configuration).

## Architecture

```
cron / OpenClaw trigger
        │
        ▼
scripts/briefing_parent.sh          ← single entrypoint
        │
        ├─ Guard 1: flock (no concurrent runs)
        ├─ Guard 2: sent-marker in state/ (no duplicate sends per day)
        │
        ├─ Collectors (parallel, each fails soft → $TMPDIR/<name>.txt)
        │     weather, sun, tides, calendar, markets, portfolio,
        │     BTC price/SMA/node, crypto news, Powerwall, EcoNet,
        │     Blink cameras, disk SMART, fail2ban, gateway status,
        │     memory/swap/zswap (delta-based, state/mem_counters.last)
        │
        ├─ scripts/compose_briefing.py  ← assembles sections + Analyst's Take
        │     (Analyst's Take generated via the LLM configured in
        │      ~/.openclaw/openclaw.json; rule-based fallback)
        │     └─ scripts/warehouse_view.py → ~/data/market.duckdb (read-only)
        │            UTC-day economics + Signal line + analyst context
        │            fail-soft → live snapshot if unavailable
        │
        ├─ scripts/render_html.py       ← plain text → minimal HTML
        │
        └─ Deliver: msmtp (email) + openclaw message send (Signal)
              └─ write sent-marker only after BOTH succeed
```

### Live snapshot vs. stored history

The brief mixes two kinds of measurement, labelled so the period is unambiguous:

- **`Live:`** — point-in-time values from `bitcoin_snapshot.sh` / `btc_sma.sh`
  (mempool depth, fee estimates, current hash rate, spot price). A stale mempool
  reading is useless, so these are always collected fresh.
- **`Day (UTC … Sat):`** — one complete UTC calendar day of block economics
  (~144 blocks, not a rolling window), read from the warehouse. The weekday is
  named because on-chain fees run ~27% lower at weekends and two of every seven
  briefs report a weekend day — unlabelled, that reads as deterioration.
- **`Signal:`** — where today sits in the warehouse's history: fee/subsidy
  percentile (weekend-corrected), apathy-streak duration, hashrate drawdown from
  its 90-day high, and exchange volume when it reaches the 95th percentile. Each
  fragment drops independently if its data is unavailable.

These same historical facts are also injected into the **LLM analyst's context**,
so the Analyst's Take reasons from where today sits in a two-year distribution
rather than from a hardcoded threshold. That is the point of the warehouse: it
informs the analysis, not just the display.

**This repo does not populate that database.** It is written by a separate
systemd ingester in the [`data_stores`](https://github.com/mikeoc61/data_stores)
repo (daily at 02:00 UTC; on-chain backfilled to 2016, price to 2013). The
briefing is strictly a read-only consumer and opens the file `read_only=True`.
If the warehouse is missing, locked, or stale the brief degrades the affected
lines and delivers normally.

## Repository layout

| Path | Purpose |
|---|---|
| `AGENTS.md` | Agent operating instructions (entrypoint, duplicate-run rules) |
| `IDENTITY.md`, `SOUL.md` | Agent persona definition |
| `HEARTBEAT.md`, `TOOLS.md` | Post-run sync workflow and environment notes |
| `briefing.env.example` | Template for local-only personal config |
| `scripts/briefing_parent.sh` | Orchestrator: guards, parallel collection, compose, deliver |
| `scripts/compose_briefing.py` | Section assembly + analytical synthesis |
| `scripts/render_html.py` | Email HTML renderer |
| `scripts/local_config.py` | Shared loader for `~/.openclaw/briefing.env` |
| `scripts/warehouse_view.py` | Read-only market_warehouse adapter: Day/Signal lines + analyst context; standalone-runnable |
| `scripts/*_snapshot.*`, others | Individual collectors (see below) |

### Collectors

| Script | Source | Output |
|---|---|---|
| `weather_snapshot.py` | Open-Meteo | Current conditions, today/tomorrow forecast, rain probability |
| `sun_snapshot.py` | api.weather.gov | Sunrise/sunset |
| `tides_snapshot.py` | tidecheck.com, NOAA CO-OPS fallback | Tide extremes |
| `calendar_snapshot.py` / `.sh` | iCloud CalDAV | Today's events from configured calendars |
| `market_snapshot.sh` | Yahoo Finance | Indexes, DXY, commodities, US 10yr yield |
| `market_calendar.py` | — | US equity session status (holiday/half-day aware) |
| `portfolio_snapshot.sh` | Yahoo Finance | Watchlist tickers, grouped by tier |
| `strc_snapshot.sh` | Yahoo Finance | STRC (Strategy Inc.) digital-credit proxy |
| `bitcoin_snapshot.sh` | Local full node (`bitcoin-cli`) | Chain state, hash rate, difficulty, peers, mempool |
| `btc_sma.sh` | CoinGecko, Binance fallback | BTC price vs 200-day SMA |
| `crypto_news.py` | CoinDesk + Bitcoin Magazine RSS | Top headlines (max 5) |
| `powerwall_snapshot.sh` | Home Assistant REST | Powerwall charge, solar, grid status |
| `econet_snapshot.sh` | Home Assistant REST | Heat pump water heater status |
| `blink_check.sh` | Home Assistant REST | Camera battery / temp / WiFi issues |
| `disk_smart.py` | Root-owned SMART snapshot | Disk health |
| `fail2ban_summary.sh` | fail2ban log (sudo-free) | SSH ban activity |
| `mem_snapshot.py` | /proc/meminfo, /proc/vmstat, zswap sysfs/debugfs | Memory/swap health, zswap-aware (see below) |

#### Memory/swap classification (`mem_snapshot.py`)

On a zswap host, `free`'s "swap used" is a context metric, not an alarm — it
decomposes into SwapCached (RAM-resident) + Zswapped (RAM, compressed) + true
on-disk pages, and here the on-disk share is ~0. The collector therefore
alarms only on *consequences* of memory pressure, as **deltas since the last
briefing** (state in `state/mem_counters.last`, keyed by `boot_id` so a reboot
resets deltas to since-boot rather than going negative):

- **crit** — `oom_kill` delta, or `MemAvailable` < 5% of RAM
- **warn** — disk swap I/O (`pswpin`/`pswpout`) > 256 MiB per interval, zswap
  writeback delta > 0, acute zswap rejects (alloc/kmemcache/reclaim fail),
  zswap pool > 80% of its cap, or `MemAvailable` < 10%

`reject_compress_fail`/`compress_poor` and small disk I/O volumes are
deliberately *not* alarmed: incompressible pages are rejected by zswap and
bypass to the swap device as routine behavior (observable as
`pswpout ≈ reject_compress_fail`). They appear in the summary as
`disk wb 0, bypass +NM` context instead. Because all alarm inputs are
monotonic kernel counters, one read per briefing catches anything that
happened overnight — no monitoring daemon required. debugfs zswap counters
are used when readable and skipped otherwise (`zswpwb`/`pswpout` in
`/proc/vmstat` cover writeback unprivileged).

Manual spot-checks: run with no argument to avoid resetting the briefing's
delta baseline; `mem_snapshot.py <state_dir>` reads *and advances* the state.

## Configuration

### Personal config — `~/.openclaw/briefing.env`

All PII (delivery targets, home coordinates, calendar names) is externalized.
Copy the template and fill in real values:

```bash
cp briefing.env.example ~/.openclaw/briefing.env
chmod 600 ~/.openclaw/briefing.env
```

The file is sourced by `briefing_parent.sh` (values with spaces **must be
double-quoted**) and read by Python collectors via `scripts/local_config.py`
(env vars take precedence over the file). The parent script hard-fails with a
clear error if the file is missing.

| Variable | Purpose |
|---|---|
| `BRIEFING_EMAIL_TO` | Email recipient |
| `BRIEFING_SIGNAL_TARGET` | Signal number (E.164, e.g. `+15555550100`) |
| `HOME_LAT` / `HOME_LON` / `HOME_ELEV_M` | Weather location |
| `BRIEFING_WEATHER_LABEL` | Header label for the weather section |
| `NWS_POINT` | `lat,lon` for api.weather.gov sunrise/sunset |
| `CALDAV_CREDS` | Path to iCloud credentials JSON |
| `CAL_INCLUDE` | Comma-separated calendar names to include |

### Secrets (local-only, never in git)

| File | Used by |
|---|---|
| `~/.openclaw/powerwall.token` | Home Assistant collectors (Powerwall, EcoNet, Blink) |
| `~/.config/openclaw/homeassistant.url` | HA URL override |
| `~/.openclaw/credentials/icloud.json` | CalDAV (`{"username": ..., "app_password": ...}`) |
| `~/.openclaw/openclaw.json` | OpenClaw gateway + LLM provider config |
| msmtp config (`~/.msmtprc`) | Email delivery |

## Host requirements

- Raspberry Pi (or any Linux host) with cron
- `bash`, `flock`, `curl`, `rsync`, `msmtp`
- Python 3.9+ (`zoneinfo`); collectors additionally use `caldav`, `icalendar`, `recurring_ical_events`
- Optional integrations: Bitcoin Core (`bitcoin-cli`), Home Assistant, `smartctl`, fail2ban (log readable via `adm` group)
- Optional: the `market_warehouse` package (from [`data_stores`](https://github.com/mikeoc61/data_stores)) for the `Day (UTC …)` on-chain history line — needs **Python 3.11+** and `duckdb`; without it the brief degrades to the live snapshot
- OpenClaw for Signal delivery and the LLM Analyst's Take

## Usage

```bash
# Normal run (cron / agent-triggered)
~/.openclaw/workspace-briefing/scripts/briefing_parent.sh

# Force a re-send after today's brief already went out
~/.openclaw/workspace-briefing/scripts/briefing_parent.sh --force
```

### Privileged SMART snapshot

On hardened hosts where the OpenClaw Gateway has `NoNewPrivileges=true`, the
briefing must not invoke `sudo`, file capabilities, or a privileged Docker
socket to read SMART data. Install `scripts/smart_snapshot_collector.py` as the
root-owned `/usr/local/libexec/openclaw-smart-snapshot`, install the two units
from `systemd/`, and define the host's stable by-id device specifications in
root-owned `/etc/openclaw-smart-snapshot.conf`:

```ini
SMART_DEVICE_1="System SSD|/dev/disk/by-id/nvme-example|nvme"
SMART_DEVICE_2="External SSD|/dev/disk/by-id/usb-example|sntasmedia"
```

Both entries are NVMe: `sntasmedia` is an NVMe-over-USB bridge, so that drive
enumerates as a SCSI node (`/dev/sda`) while `smartctl` still passes NVMe admin
commands through it. **The collector is NVMe-only** — `_summary()` in
`smart_snapshot_collector.py` reads `nvme_smart_health_information_log` and
nothing else. A genuine ATA disk (`-d sat`) is accepted by the config and then
reports `NVMe SMART health log missing` on every run; supporting one means
teaching `_summary()` to read ATA attributes.

The shipped unit sets `DevicePolicy=closed`, so it can open no disk at all until
a host-local drop-in at
`/etc/systemd/system/openclaw-smart-snapshot.service.d/devices.conf` grants one
`DeviceAllow` line per node it must reach:

```ini
[Service]
DeviceAllow=/dev/nvme0 r
DeviceAllow=/dev/nvme0n1 r
DeviceAllow=/dev/sda r
```

Substitute this host's own nodes — `readlink -f /dev/disk/by-id/<id>` resolves
each by-id path to the node to allow. For NVMe, add the controller
(`/dev/nvme0`) as well: every `nvme-*` by-id link resolves to the namespace
(`/dev/nvme0n1`), so the controller node has no by-id path of its own and is
easy to leave out. Run `sudo systemctl daemon-reload` after any edit, then
`sudo systemctl enable --now openclaw-smart-snapshot.timer`.

An existing drop-in that also sets `DevicePolicy=closed` is redundant but
harmless — it is a scalar, so the two agree.

Like `/etc/openclaw-smart-snapshot.conf`, the drop-in is deliberately **not** in
git: node names and serial-based paths are host-specific. A drive swap means
editing both files — miss the drop-in and the collector fails on a device
`DevicePolicy=closed` won't let it open.

The timer refreshes `/var/cache/openclaw-health/disk-smart.json` at 05:50 in
`Pacific/Honolulu` and shortly after boot. `disk_smart.py` accepts only a
root-owned, non-writable snapshot no more than two hours old; otherwise it
reports a warning without attempting direct disk access. Linux requires the
snapshot by default; `SMART_SNAPSHOT_REQUIRED=0` is an explicit opt-out for
standalone use on systems intentionally configured for direct SMART probing.
The collector persists only whitelisted health fields, not the full `smartctl`
payload.

#### Failure alerts

`OnFailure=openclaw-alert@%n.service` mails a report when a collection run
fails, so a broken drop-in or a dying disk does not wait for someone to notice a
stale-snapshot line in the next brief. Install `scripts/alert_unit_failure.sh`
as `/usr/local/libexec/openclaw-alert` and the `openclaw-alert@.service`
template alongside the other units.

The template runs as the briefing user rather than root — it reuses that user's
`~/.msmtprc` and reads `BRIEFING_EMAIL_TO` from `~/.openclaw/briefing.env`, the
same delivery path as the brief itself, so no credentials enter a privileged
unit. Set `User=` to the account that owns those files. Reading the failed
unit's journal also needs that account in `systemd-journal` (or `adm`);
without it the mail still arrives, just without the log excerpt.

Test it without breaking a disk:

```bash
sudo systemd-run --unit=openclaw-alert-test \
  --property=OnFailure=openclaw-alert@openclaw-alert-test.service \
  /bin/false
```

**This catches a unit that runs and fails, nothing more.** A timer that was
disabled or masked, or a host that was powered off, triggers no run and
therefore no alert — the brief's staleness warning stays the only backstop for
that case. Confirm the timer is actually armed with `systemctl list-timers
openclaw-smart-snapshot.timer` after any maintenance.

Re-invocations the same day exit 0 with "Briefing already sent today" — this
is success, not an error (see `AGENTS.md`).

### Inspecting the warehouse data standalone

`scripts/warehouse_view.py` runs on its own — useful to check what the BITCOIN
section will show without running collectors or sending a brief:

```bash
scripts/warehouse_view.py                 # day / signal / staleness lines
scripts/warehouse_view.py --json          # machine-readable
scripts/warehouse_view.py --retarget-proj=-0.79 --blocks-left=1800
```

Sample output:

```
Day (UTC 2026-07-25 Sat): 138 blks | 97% full | p50 1.0 sat/vB | fee/subsidy 0.53% | miner rev 433.5 BTC
Signal: fee/subsidy 22nd pctile 2y (7d) | apathy 19d | hashrate -12.7% off 90d high
```

Exits non-zero when the warehouse is unavailable, so it doubles as a health check.

## Tests

```bash
pytest        # needs pytest, duckdb, and the market_warehouse package
```

`conftest.py` puts `scripts/` on `sys.path` so helpers import as they do at
runtime. Coverage is currently the warehouse adapter and the memory/swap
classifier (`tests/test_mem_snapshot.py` — pure-function tests, no pi
required); collectors that hit live APIs are not unit-tested.

## Deployment workflow

This repo is the development copy — the Mac is the development system, the Pi
the deployment target. Changes are edited here, committed, and pushed; the Pi
pulls them into its own checkout at `~/projects/openclaw-briefing-agent`.

No copy step is involved: `~/.openclaw/workspace-briefing/scripts` is a symlink
to that checkout's `scripts/`, so `git pull` on the Pi *is* the deploy. The
`workspace-briefing/` directory itself holds only the non-script side — `state/`,
`*.md`, and the real `briefing.env`. After each successful run the agent rsyncs
`scripts/` and `*.md` into a mirror and pushes — see `HEARTBEAT.md` for the
exact sync steps.

Sibling helpers (`local_config.py`, `warehouse_view.py`) are imported by
directory, so they must land in the **same** `scripts/` directory as the script
importing them — sync `scripts/` as a whole rather than individual files. A
missing helper degrades silently (guarded import → fallback render) rather than
erroring, so a partial copy is easy to miss; `scripts/warehouse_view.py` run
standalone is the quickest check.

## Privacy notes

The repo is public by design, so:

- `USER.md`, `memory/`, `briefing.env`, and all token/credential files are gitignored. Runtime state lives outside the repo (`~/.openclaw/workspace-briefing/state/`), so none of it can leak via git.
- Scripts never hardcode contact info, coordinates, or tokens; they fail with an explicit message pointing at the missing config instead.
- Residual disclosure is intentionally coarse (NOAA tide station IDs and the `Pacific/Honolulu` timezone imply a region, nothing more).

## License

[MIT](LICENSE)
