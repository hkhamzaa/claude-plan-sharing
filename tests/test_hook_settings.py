from __future__ import annotations

import json
from pathlib import Path

from claude_share.integrations.claude_code.settings import (
    HOOK_COMMAND_NAME,
    HOOK_EVENT_NAME,
    install_hook,
    uninstall_hook,
)


def _all_commands(settings: dict, event_name: str = HOOK_EVENT_NAME) -> list[str]:
    groups = settings.get("hooks", {}).get(event_name, [])
    return [entry["command"] for group in groups for entry in group.get("hooks", [])]


def test_install_hook_creates_new_file_with_valid_json(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    installed = install_hook(settings_path)

    assert installed is True
    assert settings_path.exists()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert _all_commands(data) == [HOOK_COMMAND_NAME]


def test_install_hook_merges_with_existing_unrelated_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "otherSetting": True,
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "some-other-tool"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    install_hook(settings_path)

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["otherSetting"] is True
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "some-other-tool"
    assert _all_commands(data) == [HOOK_COMMAND_NAME]


def test_install_hook_is_idempotent(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    first = install_hook(settings_path)
    second = install_hook(settings_path)

    assert first is True
    assert second is False  # no-op, already present

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert _all_commands(data) == [HOOK_COMMAND_NAME]  # not duplicated


def test_install_hook_uses_custom_command_string(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    install_hook(settings_path, command="/custom/path/claude-share-hook")
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert _all_commands(data) == ["/custom/path/claude-share-hook"]


def test_uninstall_hook_removes_only_claude_share_entry(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "some-other-tool"}]}
                    ],
                    HOOK_EVENT_NAME: [{"hooks": [{"type": "command", "command": "some-other-prompt-hook"}]}],
                }
            }
        ),
        encoding="utf-8",
    )
    install_hook(settings_path)

    removed = uninstall_hook(settings_path)

    assert removed is True
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "some-other-tool"
    assert _all_commands(data) == ["some-other-prompt-hook"]


def test_uninstall_hook_returns_false_when_nothing_to_remove(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    assert uninstall_hook(settings_path) is False  # no file at all

    settings_path.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    assert uninstall_hook(settings_path) is False  # no UserPromptSubmit entry at all

    settings_path.write_text(json.dumps({}), encoding="utf-8")
    assert uninstall_hook(settings_path) is False  # no hooks key at all


def test_uninstall_hook_is_idempotent(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    install_hook(settings_path)

    first = uninstall_hook(settings_path)
    second = uninstall_hook(settings_path)

    assert first is True
    assert second is False


def test_uninstall_hook_cleans_up_empty_hooks_key(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    install_hook(settings_path)
    uninstall_hook(settings_path)

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "hooks" not in data  # nothing else was ever configured, so fully cleaned up
