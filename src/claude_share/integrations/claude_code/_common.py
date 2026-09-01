"""Shared helpers for Claude Code hook entry points."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from claude_share.agent.identity import DEFAULT_CONFIG_PATH

DEFAULT_DB_PATH = Path.home() / ".claude-share" / "claude_share.db"


def resolve_config_path() -> Path:
    env_value = os.environ.get("CLAUDE_SHARE_CONFIG")
    if env_value:
        return Path(env_value)
    return DEFAULT_CONFIG_PATH


def resolve_db_path() -> Path:
    env_value = os.environ.get("CLAUDE_SHARE_DB")
    if env_value:
        return Path(env_value)
    return DEFAULT_DB_PATH


def read_stdin_event() -> dict:
    """Best-effort read+parse of a hook's stdin JSON payload."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        import json

        return json.loads(raw)
    except Exception:
        return {}


def log_hook_error(hook_name: str, exc: BaseException) -> None:
    """Best-effort local error log for debugging a fail-open event."""
    try:
        log_path = resolve_config_path().parent / "hook.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} [{hook_name}] ")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass
