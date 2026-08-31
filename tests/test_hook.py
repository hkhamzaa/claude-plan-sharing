from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

from claude_share.agent.identity import LocalIdentity, save_local_identity
from claude_share.application.quota_service import QuotaService
from claude_share.domain.models import WindowType
from claude_share.integrations.claude_code import hook


def _run_hook(monkeypatch, config_path: Path, db_path: Path, capsys, stdin_text: str = "{}"):
    monkeypatch.setenv("CLAUDE_SHARE_CONFIG", str(config_path))
    monkeypatch.setenv("CLAUDE_SHARE_DB", str(db_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    exit_code = hook.main()
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _make_identity(pool_id, member_id, user_id) -> LocalIdentity:
    return LocalIdentity(
        pool_id=pool_id, member_id=member_id, user_id=user_id, device_id="test-device", device_name="Test Device"
    )


# --- opt-in / not configured -------------------------------------------------


def test_hook_not_logged_in_exits_0_no_output(monkeypatch, config_path: Path, db_path: Path, capsys) -> None:
    # config_path is never written to - simulates a machine that never ran `login`.
    exit_code, out, err = _run_hook(monkeypatch, config_path, db_path, capsys)
    assert exit_code == 0
    assert out == ""
    assert err == ""


def test_hook_logged_in_but_not_joined_exits_0_no_output(monkeypatch, config_path: Path, db_path: Path, capsys) -> None:
    save_local_identity(config_path, _make_identity(pool_id=None, member_id=None, user_id="some-user"))
    exit_code, out, err = _run_hook(monkeypatch, config_path, db_path, capsys)
    assert exit_code == 0
    assert out == ""
    assert err == ""


# --- decision paths ----------------------------------------------------------


def test_hook_sufficient_quota_exits_0_no_output(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    exit_code, out, err = _run_hook(monkeypatch, config_path, db_path, capsys)
    assert exit_code == 0
    assert out == ""
    assert err == ""


def test_hook_below_warning_threshold_exits_0_with_warning_on_stdout(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])  # base = 5000 each
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    # Leave 800/5000 = 16% remaining, below the 20% warning threshold, but
    # still enough to cover the placeholder per-prompt cost.
    service.consume(alice.id, WindowType.FIVE_HOUR, 4200, "warn-setup")

    exit_code, out, err = _run_hook(monkeypatch, config_path, db_path, capsys)
    assert exit_code == 0
    assert err == ""
    assert "Claude Share" in out
    assert "Quota running low." in out
    assert "Used: 42.0% / 50%" in out  # 4200/10000=42.0% used of pool; 5000/10000=50% share


def test_hook_insufficient_quota_exits_2_with_message_on_stderr(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])  # base = 5000 each
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    service.consume(alice.id, WindowType.FIVE_HOUR, 5000, "exhaust-setup")

    exit_code, out, err = _run_hook(monkeypatch, config_path, db_path, capsys)
    assert exit_code == 2
    assert out == ""
    assert "Claude Share" in err
    assert "Allocation exhausted." in err
    assert "Used: 50.0% / 50%" in err


# --- fail-open -----------------------------------------------------------------


def test_hook_corrupted_config_fails_open(monkeypatch, config_path: Path, db_path: Path, capsys) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("not valid json{{{", encoding="utf-8")

    exit_code, out, err = _run_hook(monkeypatch, config_path, db_path, capsys)
    assert exit_code == 0  # fail-open: never block on an internal error

    log_path = config_path.parent / "hook.log"
    assert log_path.exists()
    assert "JSONDecodeError" in log_path.read_text(encoding="utf-8")


def test_hook_unreachable_db_fails_open(
    monkeypatch, service: QuotaService, config_path: Path, tmp_path: Path, capsys
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    # A db path whose parent directory doesn't exist and can't be created as
    # a plain file path component - forces a real failure deep inside the
    # application/infrastructure layers, not just a missing-file no-op.
    bogus_db_path = tmp_path / "not-a-directory" / "sub" / "claude_share.db"
    (tmp_path / "not-a-directory").write_text("this is a file, not a directory", encoding="utf-8")

    exit_code, out, err = _run_hook(monkeypatch, config_path, bogus_db_path, capsys)
    assert exit_code == 0  # fail-open


def test_hook_ignores_stdin_content(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys
) -> None:
    """The hook's decision must not depend on any stdin field's value."""
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    for stdin_text in ("{}", '{"hook_event_name": "SomethingElse", "prompt": "hello"}', "not json at all", ""):
        exit_code, out, err = _run_hook(monkeypatch, config_path, db_path, capsys, stdin_text=stdin_text)
        assert exit_code == 0
        assert out == ""
        assert err == ""


# --- performance regression guard ---------------------------------------------


def test_hook_runs_within_time_budget(tmp_path: Path) -> None:
    """Regression guard: the hook must stay well under Claude Code's 30s
    UserPromptSubmit timeout. Run as a real subprocess (not an in-process
    call) so this measures actual interpreter startup + import overhead,
    matching how Claude Code actually invokes it."""
    config_path = tmp_path / "config.json"  # not logged in - the fast no-op path
    env = dict(os.environ)
    env["CLAUDE_SHARE_CONFIG"] = str(config_path)

    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "claude_share.integrations.claude_code.hook"],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0
    assert elapsed < 5.0  # generous margin under the 30s hook budget
