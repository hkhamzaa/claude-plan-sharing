"""Shared helper for the transaction-scoped advisory locks
`infrastructure/postgres/repositories.py` uses in place of `SELECT ... FOR
UPDATE`.

## Why advisory locks instead of `SELECT ... FOR UPDATE`

The first working version of this module used plain row-level
`SELECT ... FOR UPDATE` locking on QuotaWindow/CapacityGrant/CapacityRequest
rows. It gave the right answer under light concurrency, but the Milestone 1
concurrency test adapted for Postgres (`tests/test_postgres_unit_of_work.py`)
reproducibly hit `psycopg.errors.DeadlockDetected` under real contention: 10
threads with several `consume()` calls each `SELECT ... FOR UPDATE`-ing the
*same two* QuotaWindow rows (a shared pool has few members, so contention on
one member's window is the normal case, not an edge case). This isn't a bug
in the lock *ordering* - it's a documented Postgres characteristic where 3+
backends queued on `FOR UPDATE` for one frequently-updated row (each UPDATE
creates a new tuple version) can make the deadlock detector see a cycle in
the lock-wait graph that isn't a real application-level deadlock, purely as
an artifact of how row locks are threaded through MVCC tuple versions.

Postgres advisory locks (`pg_advisory_xact_lock`) don't have this failure
mode: they're plain mutexes managed by the lock manager directly, with no
tuple/MVCC involvement, so many transactions queuing on the same key is the
textbook use case they're designed for. They still participate fully in
Postgres's deadlock detector for genuine cross-resource cycles (e.g. two
transactions that really do lock two different keys in opposite orders), so
switching to them doesn't give up real deadlock protection - it only drops
the false-positive one caused by row-lock/tuple-churn interaction. See
docs/architecture.md, "Milestone 5 - Postgres locking strategy", for the
full writeup and `unit_of_work.py`'s module docstring for how this fits the
overall "mirror BEGIN IMMEDIATE's blocking behavior" design.

Locks are always taken with `pg_advisory_xact_lock` (never the non-blocking
`_try_` variant, and never the session-scoped, manually-released variant):
blocking is exactly the behavior that mirrors BEGIN IMMEDIATE, and
transaction-scoping means a lock is always released automatically at
COMMIT/ROLLBACK - even after a crash or an unhandled exception - with no
possibility of a stuck lock outliving its connection.
"""

from __future__ import annotations

import psycopg


def advisory_lock(conn: psycopg.Connection, *key_parts: str) -> None:
    """Block until this transaction holds the advisory lock identified by
    `key_parts` (joined with a separator that can't appear inside a UUID or
    enum value, so distinct logical keys can never collide by concatenation).
    Held until this transaction commits or rolls back."""
    key = "\x1f".join(key_parts)
    # hashtextextended returns a 64-bit hash, matching
    # pg_advisory_xact_lock(bigint)'s single-argument signature.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
