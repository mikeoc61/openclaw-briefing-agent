#!/usr/bin/env python3
"""Shared loader for local-only personal config (~/.openclaw/briefing.env).

Keeps PII (coordinates, contact info, calendar names) out of the public repo.
Env vars take precedence over the file. See briefing.env.example at repo root.
"""
import os
from pathlib import Path

ENV_FILE = Path.home() / ".openclaw" / "briefing.env"
_cache = None


def _load():
    global _cache
    if _cache is None:
        _cache = {}
        if ENV_FILE.is_file():
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                _cache[k.strip()] = v.strip().strip('"').strip("'")
    return _cache


def get(key, default=None):
    """Env var wins; fall back to ~/.openclaw/briefing.env, then default."""
    return os.environ.get(key) or _load().get(key) or default
