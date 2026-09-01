"""Milestone 5: drive the actual `claude-share` CLI (`cli.main:main`) in
remote mode against a real running server, proving `login --server`,
`join`, `status`, `consume`, and `capacity`/`request` all transparently
switch to talking HTTP (via `agent/remote_client.py`) instead of local
SQLite once a config file has been `login`-ed remotely - with zero changes
to how those commands are invoked from the outside (see test_cli.py for
the equivalent purely-local flow; this file exercises the same CLI, the
same `main()`, just pointed at `--server <url>`).

Backed by SQLite (not Postgres) on the server side, matching
test_server_routes.py's reasoning: this is about the CLI <-> HTTP <->
service wiring, not about Postgres-specific locking, which is already
proven separately.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from claude_share.cli.main import main
from claude_share.infrastructure.sqlite.schema import init_db
from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from claude_share.server.app import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "cli_remote_server.db"
    init_db(db_path)
    app = create_app(uow_factory=lambda: SqliteUnitOfWork(db_path))

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=0.5)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        raise RuntimeError("test server did not start in time")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def cli_config(tmp_path: Path) -> Path:
    return tmp_path / "cli-remote-config.json"


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


def test_cli_remote_end_to_end_login_join_status_consume(
    running_server: str, cli_config: Path, capsys
) -> None:
    # `pool create` targeting --server needs no prior identity at all.
    # --server is a global flag (like --db/--config) and must precede the
    # subcommand.
    exit_code = main(["--server", running_server, "pool", "create", "--name", "Remote Pool", "--members", "Alice,Bob"])
    assert exit_code == 0
    create_output = capsys.readouterr().out
    pool_id = _parse_pool_id(create_output)
    member_ids = _parse_member_ids(create_output)
    user_ids = _parse_user_ids(create_output)
    alice_id, bob_id = member_ids["Alice"], member_ids["Bob"]
    alice_user_id = user_ids["Alice"]

    # login --server registers a device against the real server and saves
    # server_url/device_token into the local identity config file.
    exit_code = main(
        [
            "--server",
            running_server,
            "--config",
            str(cli_config),
            "login",
            "--user-id",
            alice_user_id,
            "--device-name",
            "Alice's CLI",
        ]
    )
    assert exit_code == 0
    login_output = capsys.readouterr().out
    assert "against server" in login_output

    # join, over HTTP - no --db needed at all from here on since this
    # identity is remote.
    exit_code = main(["--config", str(cli_config), "join", "--pool", pool_id, "--member", alice_id])
    assert exit_code == 0
    assert "Joined pool" in capsys.readouterr().out

    # status/consume/whoami with no --member/--pool - resolved from the
    # remote-mode local identity exactly like the local-mode CLI does.
    exit_code = main(["--config", str(cli_config), "status"])
    assert exit_code == 0
    status_output = capsys.readouterr().out
    assert "allocation=5000" in status_output

    exit_code = main(
        ["--config", str(cli_config), "consume", "--window", "five_hour", "--amount", "300", "--idempotency-key", "remote-cli-1"]
    )
    assert exit_code == 0
    assert "Consumed 300 unit(s)" in capsys.readouterr().out

    exit_code = main(["--config", str(cli_config), "status"])
    assert exit_code == 0
    assert "used=300" in capsys.readouterr().out

    exit_code = main(["--config", str(cli_config), "whoami"])
    assert exit_code == 0
    whoami_output = capsys.readouterr().out
    assert f"pool_id={pool_id}" in whoami_output
    assert f"member_id={alice_id}" in whoami_output

    # Trying to act as Bob using Alice's remote identity/token is rejected
    # server-side (403) and surfaces as a clean CLI error, not a crash.
    exit_code = main(
        [
            "--config",
            str(cli_config),
            "consume",
            "--member",
            bob_id,
            "--window",
            "five_hour",
            "--amount",
            "50",
            "--idempotency-key",
            "remote-cli-2",
        ]
    )
    assert exit_code == 1
    assert "error" in capsys.readouterr().err.lower()


def test_cli_remote_capacity_delegation_round_trip(running_server: str, tmp_path: Path, capsys) -> None:
    exit_code = main(["--server", running_server, "pool", "create", "--name", "Remote Pool 2", "--members", "Alice,Bob"])
    assert exit_code == 0
    create_output = capsys.readouterr().out
    pool_id = _parse_pool_id(create_output)
    member_ids = _parse_member_ids(create_output)
    user_ids = _parse_user_ids(create_output)
    alice_id, bob_id = member_ids["Alice"], member_ids["Bob"]

    alice_config = tmp_path / "alice-config.json"
    bob_config = tmp_path / "bob-config.json"

    for config_path, user_id, member_id, name in (
        (alice_config, user_ids["Alice"], alice_id, "Alice"),
        (bob_config, user_ids["Bob"], bob_id, "Bob"),
    ):
        assert (
            main(
                [
                    "--server",
                    running_server,
                    "--config",
                    str(config_path),
                    "login",
                    "--user-id",
                    user_id,
                    "--device-name",
                    f"{name}'s CLI",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert main(["--config", str(config_path), "join", "--pool", pool_id, "--member", member_id]) == 0
        capsys.readouterr()

    # Bob asks Alice for SHARED capacity.
    exit_code = main(
        [
            "--config",
            str(bob_config),
            "request",
            "--from",
            alice_id,
            "--window",
            "five_hour",
            "--amount",
            "500",
            "--type",
            "shared",
        ]
    )
    assert exit_code == 0
    request_output = capsys.readouterr().out
    match = re.search(r"request '([^']+)'", request_output)
    assert match, request_output
    request_id = match.group(1)

    # Alice approves it, using her own remote identity as --by.
    exit_code = main(["--config", str(alice_config), "request", "approve", "--request-id", request_id])
    assert exit_code == 0
    assert "Approved" in capsys.readouterr().out

    # Alice's effective capacity, read back over HTTP, reflects the grant.
    exit_code = main(["--config", str(alice_config), "capacity", "--window", "five_hour"])
    assert exit_code == 0
    capacity_output = capsys.readouterr().out
    assert "shared_offered=500" in capacity_output
