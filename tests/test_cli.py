from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claude_share.cli.main import main


def _parse_member_ids(create_output: str) -> dict[str, str]:
    ids = {}
    for line in create_output.splitlines():
        match = re.match(r"\s*(\S+): member_id=(\S+)", line)
        if match:
            ids[match.group(1)] = match.group(2)
    return ids


def _parse_user_ids(create_output: str) -> dict[str, str]:
    ids = {}
    for line in create_output.splitlines():
        match = re.match(r"\s*(\S+): .*user_id=(\S+)", line)
        if match:
            ids[match.group(1)] = match.group(2)
    return ids


def _parse_pool_id(create_output: str) -> str:
    match = re.search(r"Created pool '([^']+)'", create_output)
    assert match, create_output
    return match.group(1)


def _parse_request_id(request_output: str) -> str:
    match = re.search(r"request '([^']+)'", request_output)
    assert match, request_output
    return match.group(1)


def _parse_grant_id(approve_output: str) -> str:
    match = re.search(r"grant '([^']+)'", approve_output)
    assert match, approve_output
    return match.group(1)


@pytest.fixture
def cli_db(tmp_path: Path) -> Path:
    return tmp_path / "cli.db"


@pytest.fixture
def cli_config(tmp_path: Path) -> Path:
    """Explicit --config path for every CLI test, so tests never touch the
    real machine's ~/.claude-share/config.json."""
    return tmp_path / "cli-config.json"


def _base_args(cli_db: Path, cli_config: Path) -> list[str]:
    return ["--db", str(cli_db), "--config", str(cli_config)]


def test_cli_end_to_end_create_status_consume(cli_db: Path, cli_config: Path, capsys) -> None:
    base = _base_args(cli_db, cli_config)

    exit_code = main([*base, "pool", "create", "--name", "CLI Pool", "--members", "Alice,Bob"])
    assert exit_code == 0
    create_output = capsys.readouterr().out
    member_ids = _parse_member_ids(create_output)
    assert set(member_ids) == {"Alice", "Bob"}
    alice_id = member_ids["Alice"]

    exit_code = main([*base, "status", "--member", alice_id])
    assert exit_code == 0
    status_output = capsys.readouterr().out
    assert "five_hour" in status_output
    assert "weekly" in status_output
    assert "allocation=5000" in status_output  # 2 members -> 5000/5000 bps

    exit_code = main(
        [*base, "consume", "--member", alice_id, "--window", "five_hour", "--amount", "10", "--idempotency-key", "cli-key-1"]
    )
    assert exit_code == 0
    consume_output = capsys.readouterr().out
    assert "Consumed 10 unit(s)" in consume_output

    exit_code = main([*base, "status", "--member", alice_id])
    assert exit_code == 0
    status_output = capsys.readouterr().out
    assert "used=10" in status_output

    # Over-allocation is rejected with a distinct exit code and no state mutation.
    exit_code = main(
        [*base, "consume", "--member", alice_id, "--window", "five_hour", "--amount", "999999", "--idempotency-key", "cli-key-2"]
    )
    assert exit_code == 2
    capsys.readouterr()

    exit_code = main([*base, "status", "--member", alice_id])
    status_output = capsys.readouterr().out
    assert "used=10" in status_output  # unchanged after the rejected consume


def test_cli_unknown_member_status_returns_error_exit_code(cli_db: Path, cli_config: Path, capsys) -> None:
    exit_code = main([*_base_args(cli_db, cli_config), "status", "--member", "nonexistent"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "not found" in err


def test_cli_solid_request_approve_and_capacity(cli_db: Path, cli_config: Path, capsys) -> None:
    base = _base_args(cli_db, cli_config)

    main([*base, "pool", "create", "--name", "CLI Pool", "--members", "Alice,Bob"])
    create_output = capsys.readouterr().out
    pool_id = _parse_pool_id(create_output)
    member_ids = _parse_member_ids(create_output)
    alice_id, bob_id = member_ids["Alice"], member_ids["Bob"]

    # `--from` is the capacity owner (Alice, who must approve); `--to` is the
    # requester/eventual recipient (Bob).
    exit_code = main(
        [*base, "request", "--pool", pool_id, "--from", alice_id, "--to", bob_id, "--window", "five_hour", "--amount", "1000", "--type", "solid"]
    )
    assert exit_code == 0
    request_output = capsys.readouterr().out
    assert "solid request" in request_output
    request_id = _parse_request_id(request_output)

    exit_code = main([*base, "request", "approve", "--request-id", request_id, "--by", alice_id])
    assert exit_code == 0
    approve_output = capsys.readouterr().out
    assert "Approved" in approve_output
    grant_id = _parse_grant_id(approve_output)

    exit_code = main([*base, "capacity", "--member", alice_id, "--window", "five_hour"])
    assert exit_code == 0
    capacity_output = capsys.readouterr().out
    assert "guaranteed_units=4000" in capacity_output  # 5000 base - 1000 sent

    exit_code = main([*base, "capacity", "--member", bob_id, "--window", "five_hour"])
    assert exit_code == 0
    capacity_output = capsys.readouterr().out
    assert "guaranteed_units=6000" in capacity_output  # 5000 base + 1000 received

    exit_code = main([*base, "grant", "revoke", "--grant-id", grant_id, "--by", alice_id])
    assert exit_code == 0
    revoke_output = capsys.readouterr().out
    assert "Revoked grant" in revoke_output

    exit_code = main([*base, "capacity", "--member", alice_id, "--window", "five_hour"])
    capacity_output = capsys.readouterr().out
    assert "guaranteed_units=5000" in capacity_output  # restored after revoke


def test_cli_request_reject_and_unauthorized_approve(cli_db: Path, cli_config: Path, capsys) -> None:
    base = _base_args(cli_db, cli_config)

    main([*base, "pool", "create", "--name", "CLI Pool", "--members", "Alice,Bob"])
    create_output = capsys.readouterr().out
    pool_id = _parse_pool_id(create_output)
    member_ids = _parse_member_ids(create_output)
    alice_id, bob_id = member_ids["Alice"], member_ids["Bob"]

    main(
        [*base, "request", "--pool", pool_id, "--from", alice_id, "--to", bob_id, "--window", "weekly", "--amount", "500", "--type", "shared"]
    )
    request_output = capsys.readouterr().out
    request_id = _parse_request_id(request_output)

    # Bob (the requester) cannot approve his own request.
    exit_code = main([*base, "request", "approve", "--request-id", request_id, "--by", bob_id])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "not authorized" in err

    exit_code = main([*base, "request", "reject", "--request-id", request_id, "--by", alice_id])
    assert exit_code == 0
    reject_output = capsys.readouterr().out
    assert "Rejected request" in reject_output


# --- Milestone 3: login / join / whoami / local-identity defaulting --------


def test_cli_whoami_not_logged_in(cli_db: Path, cli_config: Path, capsys) -> None:
    exit_code = main([*_base_args(cli_db, cli_config), "whoami"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Not logged in" in out


def test_cli_login_join_whoami_flow(cli_db: Path, cli_config: Path, capsys) -> None:
    base = _base_args(cli_db, cli_config)

    main([*base, "pool", "create", "--name", "CLI Pool", "--members", "Alice,Bob"])
    create_output = capsys.readouterr().out
    pool_id = _parse_pool_id(create_output)
    member_ids = _parse_member_ids(create_output)
    user_ids = _parse_user_ids(create_output)
    alice_id, alice_user_id = member_ids["Alice"], user_ids["Alice"]

    exit_code = main([*base, "login", "--user-id", alice_user_id, "--device-name", "Alice's Laptop"])
    assert exit_code == 0
    login_output = capsys.readouterr().out
    assert "Logged in as user_id" in login_output

    # whoami shows identity but flags "not joined" before join.
    exit_code = main([*base, "whoami"])
    assert exit_code == 0
    whoami_output = capsys.readouterr().out
    assert alice_user_id in whoami_output
    assert "Not joined" in whoami_output

    exit_code = main([*base, "join", "--pool", pool_id, "--member", alice_id])
    assert exit_code == 0
    join_output = capsys.readouterr().out
    assert "Joined pool" in join_output

    # whoami now shows the full combined identity + status + capacity view.
    exit_code = main([*base, "whoami"])
    assert exit_code == 0
    whoami_output = capsys.readouterr().out
    assert f"member_id={alice_id}" in whoami_output
    assert "guaranteed=5000" in whoami_output


def test_cli_status_uses_local_identity_when_member_omitted(cli_db: Path, cli_config: Path, capsys) -> None:
    base = _base_args(cli_db, cli_config)

    main([*base, "pool", "create", "--name", "CLI Pool", "--members", "Alice,Bob"])
    create_output = capsys.readouterr().out
    pool_id = _parse_pool_id(create_output)
    member_ids = _parse_member_ids(create_output)
    user_ids = _parse_user_ids(create_output)
    alice_id, alice_user_id = member_ids["Alice"], user_ids["Alice"]

    # Before login, status with no --member fails clearly.
    exit_code = main([*base, "status"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "no member specified" in err

    main([*base, "login", "--user-id", alice_user_id, "--device-name", "Laptop"])
    capsys.readouterr()
    main([*base, "join", "--pool", pool_id, "--member", alice_id])
    capsys.readouterr()

    # status with no --member now resolves via the local identity.
    exit_code = main([*base, "status"])
    assert exit_code == 0
    status_output = capsys.readouterr().out
    assert f"Member {alice_id!r}" in status_output


def test_cli_status_explicit_member_overrides_local_identity(cli_db: Path, cli_config: Path, capsys) -> None:
    base = _base_args(cli_db, cli_config)

    main([*base, "pool", "create", "--name", "CLI Pool", "--members", "Alice,Bob"])
    create_output = capsys.readouterr().out
    pool_id = _parse_pool_id(create_output)
    member_ids = _parse_member_ids(create_output)
    user_ids = _parse_user_ids(create_output)
    alice_id, bob_id = member_ids["Alice"], member_ids["Bob"]
    alice_user_id = user_ids["Alice"]

    main([*base, "login", "--user-id", alice_user_id, "--device-name", "Laptop"])
    capsys.readouterr()
    main([*base, "join", "--pool", pool_id, "--member", alice_id])
    capsys.readouterr()

    # Explicit --member (Bob) wins over the local identity (Alice).
    exit_code = main([*base, "status", "--member", bob_id])
    assert exit_code == 0
    status_output = capsys.readouterr().out
    assert f"Member {bob_id!r}" in status_output
    assert "Bob" in status_output


# --- Milestone 4: hook install / uninstall ---------------------------------


def test_cli_hook_install_and_uninstall_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_db: Path, cli_config: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = _base_args(cli_db, cli_config)
    settings_path = tmp_path / ".claude" / "settings.json"

    exit_code = main([*base, "hook", "install", "--project"])
    assert exit_code == 0
    install_output = capsys.readouterr().out
    assert "Installed" in install_output
    assert settings_path.exists()

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "claude-share-hook"

    # Idempotent: installing again makes no changes.
    exit_code = main([*base, "hook", "install", "--project"])
    assert exit_code == 0
    assert "already installed" in capsys.readouterr().out

    exit_code = main([*base, "hook", "uninstall", "--project"])
    assert exit_code == 0
    assert "Removed" in capsys.readouterr().out

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "hooks" not in data


def test_cli_hook_install_default_scope_is_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_db: Path, cli_config: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = _base_args(cli_db, cli_config)

    exit_code = main([*base, "hook", "install"])  # no --project/--user given
    assert exit_code == 0
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_cli_hook_uninstall_with_no_prior_install_reports_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_db: Path, cli_config: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = _base_args(cli_db, cli_config)

    exit_code = main([*base, "hook", "uninstall", "--project"])
    assert exit_code == 0
    assert "No Claude Share hook entry found" in capsys.readouterr().out
