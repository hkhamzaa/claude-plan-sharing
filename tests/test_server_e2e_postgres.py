"""Milestone 5 end-to-end test: a real `uvicorn` server, bound to a real
loopback socket, backed by a real throwaway Postgres database - register a
device, create a pool, consume quota over actual HTTP, then read it back
through a second, independent HTTP connection to prove the state was
genuinely persisted server-side rather than only visible within one
in-process test client (see tests/test_server_routes.py for the much larger
set of fast, SQLite-backed HTTP-layer tests; this file exists specifically
to prove the full stack - HTTP -> auth -> application services ->
PostgresUnitOfWork -> Postgres - works together, end to end).

Requires a reachable Postgres server - see
tests/test_postgres_unit_of_work.py's module docstring for the same
`CLAUDE_SHARE_TEST_POSTGRES_ADMIN_DSN` override and skip-if-unreachable
behavior (duplicated here rather than shared, matching this project's
existing convention of self-contained test files - see e.g.
test_quota_service.py/test_capacity_service.py each defining their own
concurrency setup rather than importing shared helpers).
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator

import httpx
import psycopg
import pytest
import uvicorn

from claude_share.infrastructure.postgres.schema import init_db
from claude_share.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from claude_share.server.app import create_app

_ADMIN_DSN = os.environ.get(
    "CLAUDE_SHARE_TEST_POSTGRES_ADMIN_DSN",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_DSN, connect_timeout=3):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason=(
        "No reachable Postgres at CLAUDE_SHARE_TEST_POSTGRES_ADMIN_DSN "
        f"({_ADMIN_DSN!r}); set that env var to run the Postgres/server e2e test."
    ),
)


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    db_name = f"claude_share_test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as admin_conn:
        admin_conn.execute(f'CREATE DATABASE "{db_name}"')

    base = _ADMIN_DSN.rsplit("/", 1)[0]
    dsn = f"{base}/{db_name}"
    init_db(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as admin_conn:
            admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(pg_dsn: str) -> Iterator[str]:
    """Starts a real uvicorn server, backed by Postgres, on a background
    thread bound to an OS-assigned loopback port. Yields its base URL."""
    port = _free_port()
    app = create_app(uow_factory=lambda: PostgresUnitOfWork(pg_dsn))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=0.5)
            break
        except httpx.TransportError as exc:
            last_error = exc
            time.sleep(0.1)
    else:
        raise RuntimeError(f"test server did not start in time: {last_error}")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_register_device_create_pool_consume_and_read_back_over_real_http(running_server: str) -> None:
    with httpx.Client(base_url=running_server, timeout=5) as client:
        r = client.get("/health")
        assert r.status_code == 200

        r = client.post("/pools", json={"name": "E2E Pool", "member_names": ["Alice", "Bob"]})
        assert r.status_code == 201, r.text
        created = r.json()
        alice = created["members"][0]

        r = client.post("/devices", json={"user_id": alice["user_id"], "device_name": "E2E Device"})
        assert r.status_code == 201, r.text
        token = r.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post(
            "/quota/consume",
            json={
                "member_id": alice["id"],
                "window_type": "five_hour",
                "amount": 250,
                "idempotency_key": "e2e-consume-1",
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] is True
        assert r.json()["remaining_units"] == 5000 - 250

    # A second, independent HTTP connection (new socket, new httpx.Client) -
    # proves the consumption was actually persisted server-side in Postgres,
    # not just visible to the connection that made it.
    with httpx.Client(base_url=running_server, timeout=5) as second_client:
        r = second_client.get(f"/members/{alice['id']}/status", headers=headers)
        assert r.status_code == 200, r.text
        status = r.json()
        assert status["member_id"] == alice["id"]
        assert status["windows"]["five_hour"]["used_units"] == 250
        assert status["windows"]["five_hour"]["remaining_units"] == 5000 - 250
