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
  ~/.openclaw/workspace-briefing/scripts \
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
