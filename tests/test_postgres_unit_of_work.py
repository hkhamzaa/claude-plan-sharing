"""Milestone 5: prove PostgresUnitOfWork gives the exact same "never oversell"
guarantee `test_quota_service.py::test_consume_concurrent_calls_never_oversell`
and `test_consume_concurrent_same_idempotency_key_consumes_once` already
proved for SQLite in Milestone 1 - now against a real Postgres database, not
a mock or an in-memory stand-in (see docs/architecture.md, "Milestone 5 -
Postgres locking strategy", for why this has to be a real database: the
whole point is proving the FOR UPDATE/advisory-lock scheme actually
serializes concurrent transactions, which no fake can demonstrate).

Requires a reachable Postgres server. Defaults to a local dev instance
(`postgresql://postgres:postgres@localhost:5432`); override with
`CLAUDE_SHARE_TEST_POSTGRES_ADMIN_DSN` (must be able to CREATE/DROP
DATABASE) if that doesn't match your environment. If no Postgres is
reachable at all, every test in this module is skipped with a clear reason
at collection time - this is a "no Postgres in this environment" escape
hatch, not a way to avoid exercising real locking when Postgres *is*
available (see also: `README.md`, "Running the Postgres/server tests").
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Iterator

import psycopg
import pytest

from claude_share.application.capacity_service import CapacityService
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import InsufficientSourceCapacityError
from claude_share.domain.models import CapacityType, WindowType
from claude_share.infrastructure.postgres.schema import init_db
from claude_share.infrastructure.postgres.unit_of_work import PostgresUnitOfWork

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
        f"({_ADMIN_DSN!r}); set that env var to run the Postgres test suite."
    ),
)


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """A freshly created, empty database with the claude-share schema
    applied - dropped again on teardown so tests never accumulate state or
    interfere with each other."""
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


@pytest.fixture
def pg_uow_factory(pg_dsn: str):
    return lambda: PostgresUnitOfWork(pg_dsn)


@pytest.fixture
def pg_service(pg_uow_factory) -> QuotaService:
    return QuotaService(uow_factory=pg_uow_factory)


@pytest.fixture
def pg_capacity_service(pg_uow_factory) -> CapacityService:
    return CapacityService(uow_factory=pg_uow_factory)


def test_consume_concurrent_calls_never_oversell(pg_service: QuotaService) -> None:
    """Same test as test_quota_service.py's SQLite version, against real
    Postgres: exactly `allocation // amount_each` of `2x` concurrent
    consume() attempts may succeed, and the persisted usage figure matches
    the accepted count exactly - no double-spend, no oversell, no lost
    update, under real FOR UPDATE row-lock contention."""
    pool = pg_service.create_pool("PG Concurrency Pool", ["Solo"])
    member = pg_service.list_members(pool.id)[0]
    allocation = pg_service.get_status(member.id).windows[WindowType.FIVE_HOUR].allocation_units

    amount_each = 100
    max_possible_successes = allocation // amount_each
    attempts = max_possible_successes * 2

    def attempt(i: int):
        return pg_service.consume(member.id, WindowType.FIVE_HOUR, amount_each, f"pg-concurrent-{i}")

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt, i) for i in range(attempts)]
        for future in as_completed(futures):
            results.append(future.result())

    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]

    assert len(accepted) == max_possible_successes
    assert len(rejected) == attempts - max_possible_successes

    status = pg_service.get_status(member.id).windows[WindowType.FIVE_HOUR]
    assert status.used_units == max_possible_successes * amount_each
    assert status.remaining_units == allocation - max_possible_successes * amount_each


def test_consume_concurrent_same_idempotency_key_consumes_once(pg_service: QuotaService, pg_dsn: str) -> None:
    """Proves the advisory-lock fix in PostgresUsageRecordRepository:
    without it, concurrent consume() calls sharing a never-before-seen
    idempotency key would race past the "does this key already exist"
    check and both attempt to INSERT, one hitting the usage_records
    UNIQUE(idempotency_key) constraint as an unhandled error instead of a
    graceful idempotent replay."""
    pool = pg_service.create_pool("PG Concurrency Pool", ["Solo"])
    member = pg_service.list_members(pool.id)[0]
    allocation = pg_service.get_status(member.id).windows[WindowType.FIVE_HOUR].allocation_units

    def attempt(_: int):
        return pg_service.consume(member.id, WindowType.FIVE_HOUR, allocation, "pg-same-key-everywhere")

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt, i) for i in range(20)]
        for future in as_completed(futures):
            results.append(future.result())

    assert all(r.accepted for r in results)
    assert all(r.remaining_units == 0 for r in results)

    status = pg_service.get_status(member.id).windows[WindowType.FIVE_HOUR]
    assert status.used_units == allocation  # consumed exactly once despite 20 concurrent calls

    with psycopg.connect(pg_dsn) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM usage_records WHERE idempotency_key = %s",
            ("pg-same-key-everywhere",),
        ).fetchone()[0]
    assert count == 1


def test_shared_concurrent_consumption_never_oversells_source(
    pg_service: QuotaService, pg_capacity_service: CapacityService
) -> None:
    """Same test as test_capacity_service.py's SQLite version: a SHARED
    grant's source is never oversold even when the source and the
    recipient are both spending against it concurrently, proving
    PostgresQuotaWindowRepository's FOR UPDATE lock also protects the
    "source window read as part of someone else's consume()" path, not
    just a member's own."""
    pool = pg_service.create_pool("PG Pool", ["Alice", "Bob"])
    alice, bob = pg_service.list_members(pool.id)
    base = pg_service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = pg_capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, base, CapacityType.SHARED
    )
    pg_capacity_service.approve_request(request.id, alice.id)
    pg_service.consume(bob.id, WindowType.FIVE_HOUR, base, "pg-bob-exhausts-own")

    amount_each = 100
    rounds = base // amount_each

    def alice_attempt(i: int):
        return pg_service.consume(alice.id, WindowType.FIVE_HOUR, amount_each, f"pg-alice-{i}")

    def bob_attempt(i: int):
        return pg_service.consume(bob.id, WindowType.FIVE_HOUR, amount_each, f"pg-bob-shared-{i}")

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for i in range(rounds):
            futures.append(executor.submit(alice_attempt, i))
            futures.append(executor.submit(bob_attempt, i))
        for future in as_completed(futures):
            results.append(future.result())

    accepted_amount = sum(r.amount for r in results if r.accepted)
    assert accepted_amount <= base

    alice_status = pg_service.get_status(alice.id).windows[WindowType.FIVE_HOUR]
    assert alice_status.used_units == accepted_amount
    assert alice_status.used_units <= base


def test_concurrent_approve_requests_only_one_shared_grant_survives(
    pg_service: QuotaService, pg_capacity_service: CapacityService
) -> None:
    """Same test as test_capacity_service.py's SQLite version: two
    concurrent approve_request() calls that would together over-commit the
    same source's capacity must not both succeed - proving the
    QuotaWindow-row lock taken inside approve_request() (before it reads
    the source's existing grants) actually serializes the two approvals."""
    pool = pg_service.create_pool("PG Pool", ["Alice", "Bob", "Carol"])
    alice, bob, carol = pg_service.list_members(pool.id)
    base = pg_service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units

    amount = (base // 2) + 100
    bob_request = pg_capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, amount, CapacityType.SHARED
    )
    carol_request = pg_capacity_service.request_capacity(
        pool.id, carol.id, alice.id, WindowType.FIVE_HOUR, amount, CapacityType.SHARED
    )

    def approve(request_id: str):
        try:
            return ("ok", pg_capacity_service.approve_request(request_id, alice.id))
        except InsufficientSourceCapacityError as exc:
            return ("rejected", exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(approve, bob_request.id), executor.submit(approve, carol_request.id)]
        outcomes = [f.result()[0] for f in as_completed(futures)]

    assert outcomes.count("ok") == 1
    assert outcomes.count("rejected") == 1

    effective = pg_capacity_service.get_effective_capacity(alice.id, WindowType.FIVE_HOUR)
    assert effective.shared_offered == amount
    assert effective.shared_offered <= base
