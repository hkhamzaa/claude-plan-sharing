"""PostgreSQL Unit of Work (Milestone 5): the same atomicity contract as
`infrastructure/sqlite/unit_of_work.py`'s `SqliteUnitOfWork`, adapted from
SQLite's whole-database `BEGIN IMMEDIATE` lock to Postgres's per-row locking
model.

## Why this isn't just "BEGIN IMMEDIATE, but Postgres"

SQLite's `BEGIN IMMEDIATE` acquires a RESERVED lock on the *entire database
file* the instant a transaction starts, before it runs a single statement -
so every read inside that transaction is implicitly guaranteed to observe a
state no other transaction can concurrently mutate. Postgres has no direct
equivalent primitive scoped to "lock the whole database up front"; the
mechanism used here to get the same "read-then-write can't race" property is
a **transaction-scoped advisory lock**
(`infrastructure/postgres/locking.py:advisory_lock()`, wrapping
`pg_advisory_xact_lock`) keyed by each row's logical identity
(`member_id`+`window_type` for a QuotaWindow, an id for a CapacityGrant/
CapacityRequest, the idempotency key for a UsageRecord lookup), taken the
moment `infrastructure/postgres/repositories.py` reads that row - not
`SELECT ... FOR UPDATE`. An earlier version of this module used plain
row-level `FOR UPDATE` locks, which is the more commonly-reached-for
Postgres idiom for this kind of problem; it was replaced after the adapted
Milestone 1 concurrency test (`tests/test_postgres_unit_of_work.py`)
reproducibly triggered `psycopg.errors.DeadlockDetected` under real
concurrent load - a documented Postgres artifact of many backends queuing
`FOR UPDATE` on one frequently-updated row, not a bug in lock ordering. See
`locking.py`'s module docstring for the full explanation. Either way, the
guarantee is the same: a second transaction that tries to read/write a
locked row blocks until the first commits or rolls back, exactly mirroring
what BEGIN IMMEDIATE already guaranteed for SQLite.

## Isolation level: READ COMMITTED (Postgres's default), not SERIALIZABLE

This was a deliberate choice, not an oversight. Postgres `SERIALIZABLE`
achieves true serializability *optimistically*: conflicting transactions are
allowed to proceed and one is aborted with a `serialization_failure` error
at COMMIT time, which the caller is expected to retry. Nothing in this
codebase's application layer (`QuotaService`/`CapacityService`) has - or,
per this milestone's brief, is allowed to grow - retry logic around
`with self._uow_factory() as uow: ... uow.commit()`; an aborted commit would
surface as an unhandled `psycopg.errors.SerializationFailure` instead of the
graceful `ConsumeResult(accepted=False, ...)` these services already return
for a losing transaction under SQLite. Explicit advisory-lock locking
achieves the same "no double-spend, no oversell" guarantee *pessimistically*
- a losing transaction blocks and then proceeds with fresh data instead of
being aborted - which is what lets the exact same, unmodified
`QuotaService.consume()`/`CapacityService.approve_request()` code paths
behave identically on both backends. See docs/architecture.md for the full
writeup, including the one known gap (a theoretical cross-locking deadlock
between two simultaneous SHARED draws in opposite directions) this trades
away in exchange for not touching application code.

## Reads still serialize too, on purpose - same tradeoff as Milestone 1

Exactly like `SqliteUnitOfWork` (see that module's docstring), this means
every read of a QuotaWindow/CapacityGrant/CapacityRequest row - not just
writes - takes a lock and can block. `get_status()`/`check_quota()`/
`get_effective_capacity()` go through the same repository methods
`consume()` does, so they serialize too. Milestone 1's docs already flagged
this as "not the right tradeoff once a central server introduces real
concurrent load... revisit this when the central server milestone is built"
- this milestone deliberately does not revisit it: preserving the proven
"never oversell" guarantee unmodified is the stated priority here, and
splitting genuinely read-only queries onto a non-locking snapshot-read path
remains a real, identified, but out-of-scope future optimization.
"""

from __future__ import annotations

import psycopg

from claude_share.domain.repository import UnitOfWork
from claude_share.infrastructure.postgres.repositories import (
    PostgresAllocationRepository,
    PostgresCapacityGrantRepository,
    PostgresCapacityRequestRepository,
    PostgresDeviceRepository,
    PostgresMemberRepository,
    PostgresPoolRepository,
    PostgresQuotaWindowRepository,
    PostgresSharedConsumptionRecordRepository,
    PostgresUsageRecordRepository,
)


class PostgresUnitOfWork(UnitOfWork):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None
        self._active = False

    def __enter__(self) -> PostgresUnitOfWork:
        # A fresh connection per transaction, exactly like SqliteUnitOfWork -
        # this is what makes it safe to call concurrently from multiple
        # threads/processes/devices without any connection-sharing races.
        # autocommit=False (psycopg's default) plus an explicit BEGIN keeps
        # the transaction boundary as visible here as SQLite's explicit
        # "BEGIN IMMEDIATE" is in its own unit_of_work.py.
        self._conn = psycopg.connect(self._dsn, autocommit=False)
        self._conn.execute("BEGIN")
        self._active = True

        self.pools = PostgresPoolRepository(self._conn)
        self.members = PostgresMemberRepository(self._conn)
        self.devices = PostgresDeviceRepository(self._conn)
        self.allocations = PostgresAllocationRepository(self._conn)
        self.windows = PostgresQuotaWindowRepository(self._conn)
        self.usage_records = PostgresUsageRecordRepository(self._conn)
        self.requests = PostgresCapacityRequestRepository(self._conn)
        self.grants = PostgresCapacityGrantRepository(self._conn)
        self.shared_consumption_records = PostgresSharedConsumptionRecordRepository(self._conn)
        return self

    def commit(self) -> None:
        if self._active:
            self._conn.execute("COMMIT")
            self._active = False

    def rollback(self) -> None:
        if self._active:
            self._conn.execute("ROLLBACK")
            self._active = False

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        try:
            if self._active:
                # Same guarantee as SqliteUnitOfWork: any path that reaches
                # here without an explicit commit() (unhandled exception, or
                # a caller that forgot to commit) is rolled back, so stored
                # state is never left partially written and every row lock
                # this transaction was holding is released immediately.
                self._conn.execute("ROLLBACK")
                self._active = False
        finally:
            assert self._conn is not None
            self._conn.close()
            self._conn = None
        return False
