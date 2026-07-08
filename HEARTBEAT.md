# HEARTBEAT.md — workspace-briefing

## Post-Run Sync Task
After each successful briefing run, sync relevant files to the main workspace mirror and push to GitHub if changes exist.

### Steps
1. Sync files to mirror:
```bash
   rsync -av --delete \
     ~/.openclaw/workspace-briefing/*.md \
     ~/.openclaw/workspace/briefing/
```
2. Stage and conditionally commit:
```bash
   cd ~/.openclaw/workspace
   git add briefing/
   git diff --cached --quiet || (git commit -m "briefing sync $(date +%Y-%m-%d): updated files" && git push origin main)
```

## Notes
- Sync runs after every briefing completion
- Commit and push only occur if mirror content changed
- `memory/` directory is excluded from sync
- Never `git init` or push directly from `workspace-briefing/`
```
