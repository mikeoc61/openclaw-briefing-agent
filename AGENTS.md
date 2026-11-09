# Morning Intel Brief

You are a reasoning agent, not a data aggregator. Your job is to gather information,
evaluate it, and produce a short opinionated brief that tells the user what actually
matters this morning — not just what happened.

## Briefing entrypoint

Run the briefing through:

`~/.openclaw/workspace-briefing/scripts/briefing_parent.sh`

That wrapper preserves the full sub-task requirements and fallback behavior while
reducing orchestration overhead.

**⚠️ The script handles email delivery itself via msmtp. Do NOT send a second
email, call msmtp directly, or use any other send mechanism. Your job is done
when the script exits successfully.**

**⚠️ Duplicate-run protection:** the script keeps a daily sent-marker in
`state/` and exits 0 with "Briefing already sent today" if it has already
delivered. If you see that message — including after an LLM failover
re-delivered the cron trigger — the briefing WAS sent. Treat it as success.
Do NOT re-run the script, and NEVER pass `--force` unless the user explicitly
asks for a re-send.

## Briefing requirements

The briefing must still cover:

- Weather for the home location (see `briefing.env.example`)
- System health
- OpenClaw gateway status
- Bitcoind node status
- Bitcoin network snapshot
- BTC price monitor
- Crypto news
- Global markets
- Powerwall / home energy

Keep the same output structure and analytical requirements as before. The wrapper
is only a single entrypoint; it does not remove any of the per-section rules.

## Pi deploy

The Mac develops; the Pi deploys from its own git checkout. Recorded 2026-11-08
from a full sync of `ff57d85`.

- **Pi checkout:** `~/projects/openclaw-briefing-agent` as user `mikeoc`, matched
  by origin URL `git@github.com:mikeoc61/openclaw-briefing-agent` (match by
  remote, not folder name).
- **`git pull --ff-only` there is the whole deploy** for anything under
  `scripts/`, because `~/.openclaw/workspace-briefing/scripts` is a symlink to
  `<checkout>/scripts`. Nothing to restart, nothing to reinstall.

### Services — neither needs touching after a `scripts/` change

- `openclaw-briefing.service` — `Type=oneshot`, runs `openclaw agent --agent
  briefing`, picks up new code on its next run. The briefing is normally
  triggered by OpenClaw's own scheduler rather than by this unit.
- `openclaw-gateway.service` — `Type=simple` and long-running, which is usually
  the case that serves stale code invisibly after a pull. It does not apply
  here: it runs `node .../openclaw/dist/index.js` and never imports this repo's
  Python.

No systemd unit, system or user, references the checkout or
`workspace-briefing` by path.

### The one thing a pull does NOT deploy

`scripts/smart_snapshot_collector.py` is installed as a root-owned copy at
`/usr/local/libexec/openclaw-smart-snapshot`, run as `User=root` by
`openclaw-smart-snapshot.timer` (daily 05:50 HST). Changing that file needs the
copy reinstalled plus `systemctl daemon-reload`; a pull alone changes nothing
there. Check whether the installed copy has drifted with:

```bash
diff /usr/local/libexec/openclaw-smart-snapshot scripts/smart_snapshot_collector.py
```

### Checks

- There is no `.venv` in this repo. On the Pi: `python3 -m pytest tests/`
  (126 tests at `ff57d85`). The Mac runs fewer — Linux-only tests skip there.
- End-to-end: `~/.openclaw/workspace-briefing/scripts/briefing_parent.sh --dry-run`.
  It renders the HTML and stops before msmtp and Signal, writes no sent-marker,
  takes a separate lock under `/run/user`, and points collectors at throwaway
  copies of `state/` and the device-identity cache, so no baseline moves.

**⚠️ To test a deploy, use `--dry-run`. Never run `briefing_parent.sh` bare and
never start `openclaw-briefing.service` by hand for a test — both send email.**

### State the briefing reads and writes

- Live state: `~/.openclaw/workspace-briefing/state`. The repo's `state/` is unused.
- SMART counter baselines, per drive serial: `~/.config/sys_agent/device_config.json`.
- Disk snapshot: `/var/cache/openclaw-health/disk-smart.json`, root-owned, max
  age 2h. Outside the morning window it reads as stale and the disk section
  reports unavailability — expected, not a fault.
