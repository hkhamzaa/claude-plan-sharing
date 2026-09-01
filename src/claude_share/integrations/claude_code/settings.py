"""Install/uninstall the Claude Share hooks in a Claude Code settings.json.

Handles merging into an existing settings.json (preserving unrelated keys
and other hooks) and is idempotent: running `install_hook` twice never
produces duplicate entries, and `uninstall_hook` only ever removes the
entries it owns.

Kept separate from `hook.py`/`stop_hook.py` (which are fast, standalone,
frequently re-invoked hook processes) since this module is only ever used
by the `claude-share hook install/uninstall` CLI commands.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Pre-prompt availability check (Milestone 4, updated Milestone 8).
USER_PROMPT_SUBMIT_EVENT = "UserPromptSubmit"
USER_PROMPT_SUBMIT_COMMAND = "claude-share-hook"

#: Post-turn token metering (Milestone 8).
STOP_EVENT = "Stop"
STOP_COMMAND = "claude-share-stop-hook"

#: Backward-compatible aliases used by existing tests/imports.
HOOK_EVENT_NAME = USER_PROMPT_SUBMIT_EVENT
HOOK_COMMAND_NAME = USER_PROMPT_SUBMIT_COMMAND
STOP_HOOK_EVENT_NAME = STOP_EVENT
STOP_HOOK_COMMAND_NAME = STOP_COMMAND

#: Every hook this integration registers. Order is stable for tests/docs.
HOOK_INSTALLATIONS: tuple[tuple[str, str], ...] = (
    (USER_PROMPT_SUBMIT_EVENT, USER_PROMPT_SUBMIT_COMMAND),
    (STOP_EVENT, STOP_COMMAND),
)


def _load_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    text = settings_path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    return json.loads(text)


def _save_settings(settings_path: Path, settings: dict) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _has_entry(event_list: list, command: str) -> bool:
    for group in event_list:
        for entry in group.get("hooks", []):
            if entry.get("type") == "command" and entry.get("command") == command:
                return True
    return False


def _install_hook_event(settings: dict, event_name: str, command: str) -> bool:
    hooks = settings.setdefault("hooks", {})
    event_list = hooks.setdefault(event_name, [])

    if _has_entry(event_list, command):
        return False

    event_list.append({"hooks": [{"type": "command", "command": command}]})
    return True


def _uninstall_hook_event(settings: dict, event_name: str, command: str) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict) or event_name not in hooks:
        return False

    event_list = hooks[event_name]
    if not isinstance(event_list, list):
        return False

    new_event_list = []
    removed = False
    for group in event_list:
        group_hooks = group.get("hooks", []) if isinstance(group, dict) else []
        kept = [h for h in group_hooks if not (h.get("type") == "command" and h.get("command") == command)]
        if len(kept) != len(group_hooks):
            removed = True

        if kept or "hooks" not in group:
            new_group = dict(group)
            if "hooks" in group:
                new_group["hooks"] = kept
            new_event_list.append(new_group)

    if not removed:
        return False

    if new_event_list:
        hooks[event_name] = new_event_list
    else:
        del hooks[event_name]
    if not hooks:
        del settings["hooks"]

    return True


def install_hook(settings_path: str | Path, command: str | None = None) -> bool:
    """Add Claude Share hook entries into `settings_path`.

    When `command` is None (the normal CLI path), installs both the
    UserPromptSubmit pre-check and the Stop post-consume hooks. When
    `command` is provided, only that single command is installed into
    UserPromptSubmit (legacy test helper behavior).

    Merges into any existing settings.json, preserving every other key and
    every other hook untouched. Returns False (no write performed) if every
    requested entry is already present — idempotent.
    """
    settings_path = Path(settings_path)
    settings = _load_settings(settings_path)

    if command is not None:
        installed = _install_hook_event(settings, USER_PROMPT_SUBMIT_EVENT, command)
    else:
        installed = False
        for event_name, hook_command in HOOK_INSTALLATIONS:
            if _install_hook_event(settings, event_name, hook_command):
                installed = True

    if not installed:
        return False

    _save_settings(settings_path, settings)
    return True


def uninstall_hook(settings_path: str | Path, command: str | None = None) -> bool:
    """Remove Claude Share hook entries from `settings_path`.

    When `command` is None, removes both UserPromptSubmit and Stop entries
    owned by this integration. When `command` is provided, removes only
    that command from UserPromptSubmit (legacy test helper behavior).

    Leaves any other configured hooks and settings.json keys intact. Returns
    False if no matching entry was found.
    """
    settings_path = Path(settings_path)
    settings = _load_settings(settings_path)

    if command is not None:
        removed = _uninstall_hook_event(settings, USER_PROMPT_SUBMIT_EVENT, command)
    else:
        removed = False
        for event_name, hook_command in HOOK_INSTALLATIONS:
            if _uninstall_hook_event(settings, event_name, hook_command):
                removed = True

    if not removed:
        return False

    _save_settings(settings_path, settings)
    return True
