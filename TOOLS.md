# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

# TOOLS.md — workspace-briefing

## Mirror & GitHub Sync
- **Source:** `~/.openclaw/workspace-briefing/` (scripts/ and *.md only)
- **Mirror location:** `~/.openclaw/workspace/briefing/`
- **Git root:** `~/.openclaw/workspace/` (one level up from mirror)
- **GitHub remote:** `git@github.com:mikeoc61/clawbot-pi.git` (SSH, key configured)
- **When to sync:** End of each successful briefing run
- **Commit only if:** Mirror content changed (enforced via `git diff --cached --quiet`)

### Sync Workflow
```bash
# Step 1 — rsync source into mirror
rsync -av --delete \
  ~/.openclaw/workspace-briefing/*.md \
  ~/.openclaw/workspace/briefing/

# Step 2 — stage, conditionally commit and push
cd ~/.openclaw/workspace
git add briefing/
git diff --cached --quiet || (git commit -m "briefing sync $(date +%Y-%m-%d): updated files" && git push origin main)
```

## Local-Only Secrets & PII
- **Powerwall token:** `~/.openclaw/powerwall.token`
- **HA URL override:** `~/.config/openclaw/homeassistant.url`
- **Personal config (PII):** `~/.openclaw/briefing.env` — delivery email, Signal
  number, home coordinates, calendar names. Template: `briefing.env.example`
  in repo root. Loaded by `briefing_parent.sh` (sourced/exported) and by
  Python collectors via `scripts/local_config.py`.
- **iCloud CalDAV creds:** `~/.openclaw/credentials/icloud.json`
- Keep all of the above out of mirror and out of git
- Briefing scripts must read secrets/PII from these files only — never hardcode
- `USER.md` and `memory/` are gitignored (personal content)

## Collectors
- **Memory/swap (`mem_snapshot.py`):** zswap-aware replacement for the old
  "swap used > 64M ⚠" heuristic in `compose_briefing.py`. Swap slot usage is
  context only (on this host: SwapCached + Zswapped, ~0 on disk). Alarms only
  on consequences — pswpin/pswpout, zswap writeback/rejects, pool >80% of cap,
  MemAvailable <10%/<5%, oom_kill — as **deltas since the last briefing** via
  `state/mem_counters.last` (same pattern as `fail2ban_sshd.last`; boot_id
  detects reboots). No monitoring daemon needed: the alarm inputs are
  monotonic counters, so a once-daily read catches anything that happened
  overnight. debugfs zswap counters are read only if permissions allow;
  otherwise `zswpwb`/`pswpout` in /proc/vmstat cover writeback. Note the brief
  reports events *between* briefings, not sub-minute timing — if a "when did
  it happen" question ever matters, that's the only case that would justify
  the systemd-timer sampler.


- **Farside ETF flows:** collector script renamed `farside_btc.py` → `farside_flows.py`.
  Now multi-asset: accepts `btc`, `eth`, and `sol` as arguments. Cache output
  filename unchanged: still writes `~/.openclaw/cache/farside_btc.json`.
  Brief stays BTC-only for now — no compose_briefing.py change needed.

  **It is NOT part of the briefing pipeline.** `briefing_parent.sh` never invokes
  it. It runs nightly under its own **user-level** systemd units —
  `farside-flows.service` + `farside-flows.timer`, managed with `systemctl --user`
  — and refreshes the cache independently. `compose_briefing.py` only *reads* that
  cache (the ETF block near L547), inside a `try`, so a missing or unreadable file
  silently drops the flows line rather than failing the brief.

  ```bash
  systemctl --user status farside-flows.timer
  ```

  User units, so: no `sudo`, and they only run while the user's systemd instance
  is alive — `loginctl enable-linger` is what keeps that true across reboots
  without an active login. If the timer ever quietly stops firing after a reboot,
  check lingering (`loginctl show-user "$USER" -p Linger`) before the unit itself.

  Because the two are decoupled, a dead collector is invisible from the briefing
  side: `farside_flows` writes its cache only after a *successful* fetch, so the
  file on disk always says `stale: false` and its `line` freezes at write time.
  `_etf_stale_note()` in `compose_briefing.py` exists for exactly this — it
  re-derives age from `as_of` at read time and appends `[STALE: data Nd old]`
  past 4 days (the threshold rides over weekends and market holidays). If that
  marker starts appearing, the collector or its schedule is the thing to check,
  not the brief.

  The collector lives in the parent workspace, not in this repo's `scripts/`.
  A duplicate copy that had accumulated here was deleted 2026-07-31 (a stray
  symlink to it went earlier, in `1e876a9`) — the live collector is unaffected.
  Don't re-add a copy: two versions with one cache path is the failure mode.
