"""PostgreSQL implementations of the domain repository ports (Milestone 5).

A structural mirror of `infrastructure/sqlite/repositories.py`: every class
here implements the exact same `domain/repository.py` port its SQLite
counterpart does, translating rows to/from the same plain dataclasses in
`domain/models.py`. Two differences from the SQLite version, both driven by
adapting SQLite's whole-database `BEGIN IMMEDIATE` lock to Postgres's
per-row locking model (see `unit_of_work.py` and docs/architecture.md,
"Milestone 5 - Postgres locking strategy", for the full reasoning):

1. `QuotaWindowRepository.get()`/`list_by_member()`, `CapacityGrantRepository
   .get()`, and `CapacityRequestRepository.get()` all take a Postgres
   transaction-scoped *advisory lock* (`locking.advisory_lock()`) keyed by
   the row's identity before reading it - not `SELECT ... FOR UPDATE`. A
   second transaction that reads/writes the same logical row via the same
   locking method blocks until the first commits or rolls back, exactly
   like `FOR UPDATE` - but see `locking.py`'s module docstring for why
   advisory locks, not row locks, are what actually make this safe under
   real contention (row locks on one hot QuotaWindow row reproducibly
   deadlocked - a documented Postgres artifact, not an ordering bug).
   `CapacityGrantRepository.list_by_source()`/`list_by_recipient()`
   deliberately do *not* take their own lock: every code path that calls
   them (`consume()`, `approve_request()`, `get_effective_capacity()`)
   already locks that member's QuotaWindow first, in the same transaction,
   which is sufficient to serialize these reads too - see those methods'
   docstrings.
2. `UsageRecordRepository.find_by_idempotency_key()` takes an advisory lock
   keyed by the idempotency key before reading. This closes a race a
   row-level lock can't: two concurrent consume() calls sharing an
   idempotency key that has *never been used before* have no existing row
   to lock onto, so without this lock both could see "not found" and both
   attempt to insert the same idempotency_key, one hitting the UNIQUE
   constraint as an unhandled error instead of the graceful
   idempotent-replay QuotaService expects.

Datetimes are stored as `TIMESTAMPTZ` and passed through directly as
timezone-aware `datetime` objects - psycopg adapts them natively, so
(unlike the SQLite repositories) there's no `.isoformat()`/`fromisoformat()`
round trip here.
"""

from __future__ import annotations

import psycopg

from claude_share.infrastructure.postgres.locking import advisory_lock

from claude_share.domain.models import (
    Allocation,
    CapacityGrant,
    CapacityRequest,
    CapacityType,
    Device,
    GrantStatus,
    Member,
    Pool,
    QuotaWindow,
    RequestStatus,
    SharedConsumptionRecord,
    UsageRecord,
    WindowType,
)
from claude_share.domain.repository import (
    AllocationRepository,
    CapacityGrantRepository,
    CapacityRequestRepository,
    DeviceRepository,
    MemberRepository,
    PoolRepository,
    QuotaWindowRepository,
    SharedConsumptionRecordRepository,
    UsageRecordRepository,
)


class PostgresPoolRepository(PoolRepository):
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, pool: Pool) -> None:
        self._conn.execute(
            "INSERT INTO pools (id, name, member_count) VALUES (%s, %s, %s)",
            (pool.id, pool.name, pool.member_count),
        )

    def get(self, pool_id: str) -> Pool | None:
        row = self._conn.execute(
            "SELECT id, name, member_count FROM pools WHERE id = %s", (pool_id,)
        ).fetchone()
        if row is None:
            return None
        return Pool(id=row[0], name=row[1], member_count=row[2])


class PostgresMemberRepository(MemberRepository):
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, member: Member) -> None:
        self._conn.execute(
            "INSERT INTO members (id, pool_id, user_id, display_name) VALUES (%s, %s, %s, %s)",
            (member.id, member.pool_id, member.user_id, member.display_name),
        )

    def get(self, member_id: str) -> Member | None:
        row = self._conn.execute(
            "SELECT id, pool_id, user_id, display_name FROM members WHERE id = %s",
            (member_id,),
        ).fetchone()
        if row is None:
            return None
        return Member(id=row[0], pool_id=row[1], user_id=row[2], display_name=row[3])

    def list_by_pool(self, pool_id: str) -> list[Member]:
        # ORDER BY seq (insertion order), not id (a random UUID) - see the
        # `seq` column's comment in schema.py.
        rows = self._conn.execute(
            "SELECT id, pool_id, user_id, display_name FROM members WHERE pool_id = %s ORDER BY seq",
            (pool_id,),
        ).fetchall()
        return [Member(id=r[0], pool_id=r[1], user_id=r[2], display_name=r[3]) for r in rows]

    def find_by_user_id(self, user_id: str) -> Member | None:
        row = self._conn.execute(
            "SELECT id, pool_id, user_id, display_name FROM members WHERE user_id = %s LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return Member(id=row[0], pool_id=row[1], user_id=row[2], display_name=row[3])


class PostgresDeviceRepository(DeviceRepository):
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, device: Device) -> None:
        self._conn.execute(
            "INSERT INTO devices (id, user_id, device_name, created_at, token_hash) VALUES (%s, %s, %s, %s, %s)",
            (device.id, device.user_id, device.device_name, device.created_at, device.token_hash),
        )

    def get(self, device_id: str) -> Device | None:
        row = self._conn.execute(
            "SELECT id, user_id, device_name, created_at, token_hash FROM devices WHERE id = %s",
            (device_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_device(row)

    def list_by_user(self, user_id: str) -> list[Device]:
        rows = self._conn.execute(
            "SELECT id, user_id, device_name, created_at, token_hash FROM devices WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [_row_to_device(row) for row in rows]

    def find_by_token_hash(self, token_hash: str) -> Device | None:
        row = self._conn.execute(
            "SELECT id, user_id, device_name, created_at, token_hash FROM devices WHERE token_hash = %s",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_device(row)


def _row_to_device(row: tuple) -> Device:
    return Device(id=row[0], user_id=row[1], device_name=row[2], created_at=row[3], token_hash=row[4])


class PostgresAllocationRepository(AllocationRepository):
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, allocation: Allocation) -> None:
        self._conn.execute(
            "INSERT INTO allocations (member_id, bps) VALUES (%s, %s)",
            (allocation.member_id, allocation.bps),
        )

    def get(self, member_id: str) -> Allocation | None:
        row = self._conn.execute(
            "SELECT member_id, bps FROM allocations WHERE member_id = %s", (member_id,)
        ).fetchone()
        if row is None:
            return None
        return Allocation(member_id=row[0], bps=row[1])


class PostgresQuotaWindowRepository(QuotaWindowRepository):
    """`get`/`list_by_member` take an advisory lock before reading - see
    module docstring point 1 and `locking.py`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, window: QuotaWindow) -> None:
        self._conn.execute(
            """
            INSERT INTO quota_windows
                (member_id, window_type, window_start, reset_at, allocation_units, usage_units)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                window.member_id,
                window.window_type.value,
                window.window_start,
                window.reset_at,
                window.allocation_units,
                window.usage_units,
            ),
        )

    def get(self, member_id: str, window_type: WindowType) -> QuotaWindow | None:
        advisory_lock(self._conn, "quota_window", member_id, window_type.value)
        row = self._conn.execute(
            """
            SELECT member_id, window_type, window_start, reset_at, allocation_units, usage_units
            FROM quota_windows
            WHERE member_id = %s AND window_type = %s
            """,
            (member_id, window_type.value),
        ).fetchone()
        if row is None:
            return None
        return _row_to_window(row)

    def update(self, window: QuotaWindow) -> None:
        cursor = self._conn.execute(
            """
            UPDATE quota_windows
            SET window_start = %s, reset_at = %s, allocation_units = %s, usage_units = %s
            WHERE member_id = %s AND window_type = %s
            """,
            (
                window.window_start,
                window.reset_at,
                window.allocation_units,
                window.usage_units,
                window.member_id,
                window.window_type.value,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(
                f"No quota_windows row for member={window.member_id!r} "
                f"window_type={window.window_type.value!r} to update"
            )

    def list_by_member(self, member_id: str) -> list[QuotaWindow]:
        # Lock every WindowType this member could have a row for - a fixed,
        # small set (currently 2), so this is cheap - rather than locking
        # only whichever window_types actually come back from the query.
        for window_type in WindowType:
            advisory_lock(self._conn, "quota_window", member_id, window_type.value)
        rows = self._conn.execute(
            """
            SELECT member_id, window_type, window_start, reset_at, allocation_units, usage_units
            FROM quota_windows
            WHERE member_id = %s
            """,
            (member_id,),
        ).fetchall()
        return [_row_to_window(row) for row in rows]


class PostgresUsageRecordRepository(UsageRecordRepository):
    """`find_by_idempotency_key` takes an advisory lock - see module docstring point 2."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, record: UsageRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO usage_records
                (id, member_id, window_type, amount, idempotency_key, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                record.id,
                record.member_id,
                record.window_type.value,
                record.amount,
                record.idempotency_key,
                record.timestamp,
            ),
        )

    def find_by_idempotency_key(self, idempotency_key: str) -> UsageRecord | None:
        advisory_lock(self._conn, "idempotency_key", idempotency_key)
        row = self._conn.execute(
            """
            SELECT id, member_id, window_type, amount, idempotency_key, timestamp
            FROM usage_records
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return UsageRecord(
            id=row[0],
            member_id=row[1],
            window_type=WindowType(row[2]),
            amount=row[3],
            idempotency_key=row[4],
            timestamp=row[5],
        )


def _row_to_window(row: tuple) -> QuotaWindow:
    return QuotaWindow(
        member_id=row[0],
        window_type=WindowType(row[1]),
        window_start=row[2],
        reset_at=row[3],
        allocation_units=row[4],
        usage_units=row[5],
    )


class PostgresCapacityRequestRepository(CapacityRequestRepository):
    """`get` takes an advisory lock before reading - see module docstring
    point 1 and `locking.py`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, request: CapacityRequest) -> None:
        self._conn.execute(
            """
            INSERT INTO capacity_requests
                (id, pool_id, requester_member_id, target_member_id, window_type,
                 amount, type, status, created_at, approved_at, expires_at, message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                request.id,
                request.pool_id,
                request.requester_member_id,
                request.target_member_id,
                request.window_type.value,
                request.amount,
                request.type.value,
                request.status.value,
                request.created_at,
                request.approved_at,
                request.expires_at,
                request.message,
            ),
        )

    def get(self, request_id: str) -> CapacityRequest | None:
        advisory_lock(self._conn, "capacity_request", request_id)
        row = self._conn.execute(
            """
            SELECT id, pool_id, requester_member_id, target_member_id, window_type,
                   amount, type, status, created_at, approved_at, expires_at, message
            FROM capacity_requests
            WHERE id = %s
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_request(row)

    def update(self, request: CapacityRequest) -> None:
        cursor = self._conn.execute(
            """
            UPDATE capacity_requests
            SET status = %s, approved_at = %s, expires_at = %s, message = %s
            WHERE id = %s
            """,
            (
                request.status.value,
                request.approved_at,
                request.expires_at,
                request.message,
                request.id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No capacity_requests row with id={request.id!r} to update")


def _row_to_request(row: tuple) -> CapacityRequest:
    return CapacityRequest(
        id=row[0],
        pool_id=row[1],
        requester_member_id=row[2],
        target_member_id=row[3],
        window_type=WindowType(row[4]),
        amount=row[5],
        type=CapacityType(row[6]),
        status=RequestStatus(row[7]),
        created_at=row[8],
        approved_at=row[9],
        expires_at=row[10],
        message=row[11],
    )


class PostgresCapacityGrantRepository(CapacityGrantRepository):
    """`get` takes an advisory lock before reading (protects
    `revoke_grant()`'s get-then-update against a concurrent revoke of the
    same grant). `list_by_source`/`list_by_recipient` deliberately do not -
    see module docstring point 1 for why the QuotaWindow lock already taken
    earlier in every caller of these two methods is sufficient."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, grant: CapacityGrant) -> None:
        self._conn.execute(
            """
            INSERT INTO capacity_grants
                (id, pool_id, source_member_id, recipient_member_id, window_type,
                 amount, type, status, created_at, activated_at, expires_at, revoked_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                grant.id,
                grant.pool_id,
                grant.source_member_id,
                grant.recipient_member_id,
                grant.window_type.value,
                grant.amount,
                grant.type.value,
                grant.status.value,
                grant.created_at,
                grant.activated_at,
                grant.expires_at,
                grant.revoked_at,
            ),
        )

    def get(self, grant_id: str) -> CapacityGrant | None:
        advisory_lock(self._conn, "capacity_grant", grant_id)
        row = self._conn.execute(
            """
            SELECT id, pool_id, source_member_id, recipient_member_id, window_type,
                   amount, type, status, created_at, activated_at, expires_at, revoked_at
            FROM capacity_grants
            WHERE id = %s
            """,
            (grant_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_grant(row)

    def update(self, grant: CapacityGrant) -> None:
        cursor = self._conn.execute(
            """
            UPDATE capacity_grants
            SET status = %s, revoked_at = %s
            WHERE id = %s
            """,
            (grant.status.value, grant.revoked_at, grant.id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No capacity_grants row with id={grant.id!r} to update")

    def list_by_source(self, member_id: str, window_type: WindowType) -> list[CapacityGrant]:
        rows = self._conn.execute(
            """
            SELECT id, pool_id, source_member_id, recipient_member_id, window_type,
                   amount, type, status, created_at, activated_at, expires_at, revoked_at
            FROM capacity_grants
            WHERE source_member_id = %s AND window_type = %s
            """,
            (member_id, window_type.value),
        ).fetchall()
        return [_row_to_grant(row) for row in rows]

    def list_by_recipient(self, member_id: str, window_type: WindowType) -> list[CapacityGrant]:
        rows = self._conn.execute(
            """
            SELECT id, pool_id, source_member_id, recipient_member_id, window_type,
                   amount, type, status, created_at, activated_at, expires_at, revoked_at
            FROM capacity_grants
            WHERE recipient_member_id = %s AND window_type = %s
            """,
            (member_id, window_type.value),
        ).fetchall()
        return [_row_to_grant(row) for row in rows]


def _row_to_grant(row: tuple) -> CapacityGrant:
    return CapacityGrant(
        id=row[0],
        pool_id=row[1],
        source_member_id=row[2],
        recipient_member_id=row[3],
        window_type=WindowType(row[4]),
        amount=row[5],
        type=CapacityType(row[6]),
        status=GrantStatus(row[7]),
        created_at=row[8],
        activated_at=row[9],
        expires_at=row[10],
        revoked_at=row[11],
    )


class PostgresSharedConsumptionRecordRepository(SharedConsumptionRecordRepository):
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def add(self, record: SharedConsumptionRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO shared_consumption_records
                (id, usage_record_id, grant_id, amount, timestamp)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (record.id, record.usage_record_id, record.grant_id, record.amount, record.timestamp),
        )

    def list_by_usage_record(self, usage_record_id: str) -> list[SharedConsumptionRecord]:
        rows = self._conn.execute(
            """
            SELECT id, usage_record_id, grant_id, amount, timestamp
            FROM shared_consumption_records
            WHERE usage_record_id = %s
            """,
            (usage_record_id,),
        ).fetchall()
        return [_row_to_shared_consumption_record(row) for row in rows]

    def list_by_grant(self, grant_id: str) -> list[SharedConsumptionRecord]:
        rows = self._conn.execute(
            """
            SELECT id, usage_record_id, grant_id, amount, timestamp
            FROM shared_consumption_records
            WHERE grant_id = %s
            """,
            (grant_id,),
        ).fetchall()
        return [_row_to_shared_consumption_record(row) for row in rows]


def _row_to_shared_consumption_record(row: tuple) -> SharedConsumptionRecord:
    return SharedConsumptionRecord(
        id=row[0], usage_record_id=row[1], grant_id=row[2], amount=row[3], timestamp=row[4]
    )
