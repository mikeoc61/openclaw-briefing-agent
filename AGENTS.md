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
