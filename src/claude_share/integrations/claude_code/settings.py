"""Install/uninstall the Claude Share hook in a Claude Code settings.json.

Handles merging into an existing settings.json (preserving unrelated keys
and other hooks) and is idempotent: running `install_hook` twice never
produces duplicate entries, and `uninstall_hook` only ever removes the
entry it is looking for.

Kept separate from `hook.py` (which is the fast, standalone, frequently
re-invoked hook process) since this module is only ever used by the
`claude-share hook install/uninstall` CLI commands - a completely
different runtime path with no latency budget to worry about.
"""

from __future__ import annotations

import json
from pathlib import Path

#: The Claude Code hook event this integration registers against. See
#: hook.py's module docstring for the verified UserPromptSubmit contract.
HOOK_EVENT_NAME = "UserPromptSubmit"

#: The console-script name installed by this package (see pyproject.toml
#: [project.scripts]). Written into settings.json as a bare command name,
#: not a hardcoded absolute path, so the same settings.json works on any
#: machine that has claude-share installed (resolved via PATH at hook
#: invocation time, same as any other shell command).
HOOK_COMMAND_NAME = "claude-share-hook"


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


def install_hook(settings_path: str | Path, command: str = HOOK_COMMAND_NAME) -> bool:
    """Add a UserPromptSubmit hook entry pointing at `command`.

    Merges into any existing settings.json, preserving every other key and
    every other hook untouched. Returns False (no write performed) if an
    entry for `command` is already present - idempotent.
    """
    settings_path = Path(settings_path)
    settings = _load_settings(settings_path)

    hooks = settings.setdefault("hooks", {})
    event_list = hooks.setdefault(HOOK_EVENT_NAME, [])

    if _has_entry(event_list, command):
        return False

    event_list.append({"hooks": [{"type": "command", "command": command}]})
    _save_settings(settings_path, settings)
    return True


def uninstall_hook(settings_path: str | Path, command: str = HOOK_COMMAND_NAME) -> bool:
    """Remove only the `command` entry from the UserPromptSubmit hook list.

    Leaves any other configured hooks (for this event or any other) and
    any other settings.json keys completely intact. Returns False (no
    write performed) if no matching entry was found.
    """
    settings_path = Path(settings_path)
    settings = _load_settings(settings_path)

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict) or HOOK_EVENT_NAME not in hooks:
        return False

    event_list = hooks[HOOK_EVENT_NAME]
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
            # Keep the group if it still has other hooks, or if it never
            # had a "hooks" key at all (leave whatever we don't understand
            # untouched rather than dropping it).
            new_group = dict(group)
            if "hooks" in group:
                new_group["hooks"] = kept
            new_event_list.append(new_group)
        # else: the group's hooks became empty solely because we removed
        # our own entry from it - drop the now-pointless group entirely.

    if not removed:
        return False

    if new_event_list:
        hooks[HOOK_EVENT_NAME] = new_event_list
    else:
        del hooks[HOOK_EVENT_NAME]
    if not hooks:
        del settings["hooks"]

    _save_settings(settings_path, settings)
    return True
