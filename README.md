# OpenClaw Morning Briefing Agent

A self-hosted morning intelligence briefing that runs on a Raspberry Pi and
delivers a single, opinionated daily brief via **HTML email** (msmtp) and
**Signal** (OpenClaw messaging). It aggregates weather, tides, markets,
crypto/on-chain data, home energy, and system health — then synthesizes an
analyst's take rather than just dumping data.

Built as a workspace for an OpenClaw agent triggered by cron each morning,
but the pipeline is plain bash + Python and runs standalone.

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
        │     Blink cameras, disk SMART, fail2ban, gateway status
        │
        ├─ scripts/compose_briefing.py  ← assembles sections + Analyst's Take
        │     (Analyst's Take generated via the LLM configured in
        │      ~/.openclaw/openclaw.json; rule-based fallback)
        │
        ├─ scripts/render_html.py       ← plain text → minimal HTML
        │
        └─ Deliver: msmtp (email) + openclaw message send (Signal)
              └─ write sent-marker only after BOTH succeed
```

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
| `portfolio_snapshot.sh` | Yahoo Finance | Watchlist tickers, grouped by conviction tier |
| `strc_snapshot.sh` | Yahoo Finance | STRC (Strategy Inc.) digital-credit proxy |
| `bitcoin_snapshot.sh` | Local full node (`bitcoin-cli`) | Chain state, hash rate, difficulty, peers, mempool |
| `btc_sma.sh` | CoinGecko, Binance fallback | BTC price vs 200-day SMA |
| `crypto_news.py` | CoinDesk + Bitcoin Magazine RSS | Top headlines (max 5) |
| `powerwall_snapshot.sh` | Home Assistant REST | Powerwall charge, solar, grid status |
| `econet_snapshot.sh` | Home Assistant REST | Heat pump water heater status |
| `blink_check.sh` | Home Assistant REST | Camera battery / temp / WiFi issues |
| `disk_smart.py` | smartctl | Disk health |
| `fail2ban_summary.sh` | fail2ban log (sudo-free) | SSH ban activity |

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
- Optional integrations: Bitcoin Core (`bitcoin-cli`), Home Assistant, `smartctl` (NOPASSWD sudoers entry), fail2ban (log readable via `adm` group)
- OpenClaw for Signal delivery and the LLM Analyst's Take

## Usage

```bash
# Normal run (cron / agent-triggered)
~/.openclaw/workspace-briefing/scripts/briefing_parent.sh

# Force a re-send after today's brief already went out
~/.openclaw/workspace-briefing/scripts/briefing_parent.sh --force
```

Re-invocations the same day exit 0 with "Briefing already sent today" — this
is success, not an error (see `AGENTS.md`).

## Deployment workflow

This repo is the development copy. Changes are edited here, then copied to the
Pi host (`~/.openclaw/workspace-briefing/`). After each successful run the
agent rsyncs `scripts/` and `*.md` into a mirror and pushes — see
`HEARTBEAT.md` for the exact sync steps.

## Privacy notes

The repo is public by design, so:

- `USER.md`, `memory/`, `state/`, `briefing.env`, and all token/credential files are gitignored.
- Scripts never hardcode contact info, coordinates, or tokens; they fail with an explicit message pointing at the missing config instead.
- Residual disclosure is intentionally coarse (NOAA tide station IDs and the `Pacific/Honolulu` timezone imply a region, nothing more).

## License

[MIT](LICENSE)
