"""SQLite Unit of Work: real transactional/locking guarantees, not read-modify-write.

Each `with SqliteUnitOfWork(db_path) as uow:` block opens its own SQLite
connection and immediately issues `BEGIN IMMEDIATE`, which acquires SQLite's
RESERVED lock up front. That means the moment a transaction starts reading
a member's remaining quota, no other connection (thread, process, or in the
future, another device) can start a competing write transaction on this
database until this one commits or rolls back - the classic
check-then-write race in `consume()` is closed at the database level, not
just in application code.

Opening a fresh connection per transaction (rather than sharing one
connection across calls) is deliberate: it is what makes this safe to call
concurrently from multiple threads or processes later, since SQLite's
locking is enforced at the file level, per connection/transaction.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from claude_share.domain.repository import UnitOfWork
from claude_share.infrastructure.sqlite.repositories import (
    SqliteAllocationRepository,
    SqliteCapacityGrantRepository,
    SqliteCapacityRequestRepository,
    SqliteDeviceRepository,
    SqliteMemberRepository,
    SqlitePoolRepository,
    SqliteQuotaWindowRepository,
    SqliteSharedConsumptionRecordRepository,
    SqliteUsageRecordRepository,
)

#: How long (seconds) a transaction will wait to acquire the write lock
#: before raising sqlite3.OperationalError("database is locked").
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0


class SqliteUnitOfWork(UnitOfWork):
    def __init__(self, db_path: str | Path, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self._db_path = str(db_path)
        self._timeout = timeout
        self._conn: sqlite3.Connection | None = None
        self._active = False

    def __enter__(self) -> SqliteUnitOfWork:
        # isolation_level=None puts sqlite3 into autocommit mode, so we can
        # issue our own explicit BEGIN IMMEDIATE instead of the driver's
        # default deferred BEGIN (which would only take the lock on the
        # first write, reopening the same race we need to avoid).
        self._conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=self._timeout)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("BEGIN IMMEDIATE")
        self._active = True

        self.pools = SqlitePoolRepository(self._conn)
        self.members = SqliteMemberRepository(self._conn)
        self.devices = SqliteDeviceRepository(self._conn)
        self.allocations = SqliteAllocationRepository(self._conn)
        self.windows = SqliteQuotaWindowRepository(self._conn)
        self.usage_records = SqliteUsageRecordRepository(self._conn)
        self.requests = SqliteCapacityRequestRepository(self._conn)
        self.grants = SqliteCapacityGrantRepository(self._conn)
        self.shared_consumption_records = SqliteSharedConsumptionRecordRepository(self._conn)
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
                # Any path that reaches here without an explicit commit()
                # (an unhandled exception, or a caller that forgot to
                # commit) is rolled back, so stored state is never left
                # partially written.
                self._conn.execute("ROLLBACK")
                self._active = False
        finally:
            assert self._conn is not None
            self._conn.close()
            self._conn = None
        return False
