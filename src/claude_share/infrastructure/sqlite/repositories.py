"""SQLite implementations of the domain repository ports.

Each repository operates on a single `sqlite3.Connection` that is owned and
transaction-scoped by `SqliteUnitOfWork`. Rows are translated to/from the
plain domain dataclasses at the boundary so that no `sqlite3.Row`, SQL, or
connection object ever leaks into the domain or application layers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

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


class SqlitePoolRepository(PoolRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, pool: Pool) -> None:
        self._conn.execute(
            "INSERT INTO pools (id, name, member_count) VALUES (?, ?, ?)",
            (pool.id, pool.name, pool.member_count),
        )

    def get(self, pool_id: str) -> Pool | None:
        row = self._conn.execute(
            "SELECT id, name, member_count FROM pools WHERE id = ?", (pool_id,)
        ).fetchone()
        if row is None:
            return None
        return Pool(id=row[0], name=row[1], member_count=row[2])


class SqliteMemberRepository(MemberRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, member: Member) -> None:
        self._conn.execute(
            "INSERT INTO members (id, pool_id, user_id, display_name) VALUES (?, ?, ?, ?)",
            (member.id, member.pool_id, member.user_id, member.display_name),
        )

    def get(self, member_id: str) -> Member | None:
        row = self._conn.execute(
            "SELECT id, pool_id, user_id, display_name FROM members WHERE id = ?",
            (member_id,),
        ).fetchone()
        if row is None:
            return None
        return Member(id=row[0], pool_id=row[1], user_id=row[2], display_name=row[3])

    def list_by_pool(self, pool_id: str) -> list[Member]:
        rows = self._conn.execute(
            "SELECT id, pool_id, user_id, display_name FROM members WHERE pool_id = ? ORDER BY rowid",
            (pool_id,),
        ).fetchall()
        return [Member(id=r[0], pool_id=r[1], user_id=r[2], display_name=r[3]) for r in rows]

    def find_by_user_id(self, user_id: str) -> Member | None:
        row = self._conn.execute(
            "SELECT id, pool_id, user_id, display_name FROM members WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return Member(id=row[0], pool_id=row[1], user_id=row[2], display_name=row[3])


class SqliteDeviceRepository(DeviceRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, device: Device) -> None:
        self._conn.execute(
            "INSERT INTO devices (id, user_id, device_name, created_at) VALUES (?, ?, ?, ?)",
            (device.id, device.user_id, device.device_name, device.created_at.isoformat()),
        )

    def get(self, device_id: str) -> Device | None:
        row = self._conn.execute(
            "SELECT id, user_id, device_name, created_at FROM devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_device(row)

    def list_by_user(self, user_id: str) -> list[Device]:
        rows = self._conn.execute(
            "SELECT id, user_id, device_name, created_at FROM devices WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [_row_to_device(row) for row in rows]


def _row_to_device(row: tuple) -> Device:
    return Device(
        id=row[0],
        user_id=row[1],
        device_name=row[2],
        created_at=datetime.fromisoformat(row[3]),
    )


class SqliteAllocationRepository(AllocationRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, allocation: Allocation) -> None:
        self._conn.execute(
            "INSERT INTO allocations (member_id, bps) VALUES (?, ?)",
            (allocation.member_id, allocation.bps),
        )

    def get(self, member_id: str) -> Allocation | None:
        row = self._conn.execute(
            "SELECT member_id, bps FROM allocations WHERE member_id = ?", (member_id,)
        ).fetchone()
        if row is None:
            return None
        return Allocation(member_id=row[0], bps=row[1])


class SqliteQuotaWindowRepository(QuotaWindowRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, window: QuotaWindow) -> None:
        self._conn.execute(
            """
            INSERT INTO quota_windows
                (member_id, window_type, window_start, reset_at, allocation_units, usage_units)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                window.member_id,
                window.window_type.value,
                window.window_start.isoformat(),
                window.reset_at.isoformat(),
                window.allocation_units,
                window.usage_units,
            ),
        )

    def get(self, member_id: str, window_type: WindowType) -> QuotaWindow | None:
        row = self._conn.execute(
            """
            SELECT member_id, window_type, window_start, reset_at, allocation_units, usage_units
            FROM quota_windows
            WHERE member_id = ? AND window_type = ?
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
            SET window_start = ?, reset_at = ?, allocation_units = ?, usage_units = ?
            WHERE member_id = ? AND window_type = ?
            """,
            (
                window.window_start.isoformat(),
                window.reset_at.isoformat(),
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
        rows = self._conn.execute(
            """
            SELECT member_id, window_type, window_start, reset_at, allocation_units, usage_units
            FROM quota_windows
            WHERE member_id = ?
            """,
            (member_id,),
        ).fetchall()
        return [_row_to_window(row) for row in rows]


class SqliteUsageRecordRepository(UsageRecordRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, record: UsageRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO usage_records
                (id, member_id, window_type, amount, idempotency_key, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.member_id,
                record.window_type.value,
                record.amount,
                record.idempotency_key,
                record.timestamp.isoformat(),
            ),
        )

    def find_by_idempotency_key(self, idempotency_key: str) -> UsageRecord | None:
        row = self._conn.execute(
            """
            SELECT id, member_id, window_type, amount, idempotency_key, timestamp
            FROM usage_records
            WHERE idempotency_key = ?
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
            timestamp=datetime.fromisoformat(row[5]),
        )


def _row_to_window(row: tuple) -> QuotaWindow:
    return QuotaWindow(
        member_id=row[0],
        window_type=WindowType(row[1]),
        window_start=datetime.fromisoformat(row[2]),
        reset_at=datetime.fromisoformat(row[3]),
        allocation_units=row[4],
        usage_units=row[5],
    )


class SqliteCapacityRequestRepository(CapacityRequestRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, request: CapacityRequest) -> None:
        self._conn.execute(
            """
            INSERT INTO capacity_requests
                (id, pool_id, requester_member_id, target_member_id, window_type,
                 amount, type, status, created_at, approved_at, expires_at, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                request.created_at.isoformat(),
                request.approved_at.isoformat() if request.approved_at else None,
                request.expires_at.isoformat() if request.expires_at else None,
                request.message,
            ),
        )

    def get(self, request_id: str) -> CapacityRequest | None:
        row = self._conn.execute(
            """
            SELECT id, pool_id, requester_member_id, target_member_id, window_type,
                   amount, type, status, created_at, approved_at, expires_at, message
            FROM capacity_requests
            WHERE id = ?
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
            SET status = ?, approved_at = ?, expires_at = ?, message = ?
            WHERE id = ?
            """,
            (
                request.status.value,
                request.approved_at.isoformat() if request.approved_at else None,
                request.expires_at.isoformat() if request.expires_at else None,
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
        created_at=datetime.fromisoformat(row[8]),
        approved_at=datetime.fromisoformat(row[9]) if row[9] else None,
        expires_at=datetime.fromisoformat(row[10]) if row[10] else None,
        message=row[11],
    )


class SqliteCapacityGrantRepository(CapacityGrantRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, grant: CapacityGrant) -> None:
        self._conn.execute(
            """
            INSERT INTO capacity_grants
                (id, pool_id, source_member_id, recipient_member_id, window_type,
                 amount, type, status, created_at, activated_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                grant.created_at.isoformat(),
                grant.activated_at.isoformat(),
                grant.expires_at.isoformat(),
                grant.revoked_at.isoformat() if grant.revoked_at else None,
            ),
        )

    def get(self, grant_id: str) -> CapacityGrant | None:
        row = self._conn.execute(
            """
            SELECT id, pool_id, source_member_id, recipient_member_id, window_type,
                   amount, type, status, created_at, activated_at, expires_at, revoked_at
            FROM capacity_grants
            WHERE id = ?
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
            SET status = ?, revoked_at = ?
            WHERE id = ?
            """,
            (
                grant.status.value,
                grant.revoked_at.isoformat() if grant.revoked_at else None,
                grant.id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No capacity_grants row with id={grant.id!r} to update")

    def list_by_source(self, member_id: str, window_type: WindowType) -> list[CapacityGrant]:
        rows = self._conn.execute(
            """
            SELECT id, pool_id, source_member_id, recipient_member_id, window_type,
                   amount, type, status, created_at, activated_at, expires_at, revoked_at
            FROM capacity_grants
            WHERE source_member_id = ? AND window_type = ?
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
            WHERE recipient_member_id = ? AND window_type = ?
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
        created_at=datetime.fromisoformat(row[8]),
        activated_at=datetime.fromisoformat(row[9]),
        expires_at=datetime.fromisoformat(row[10]),
        revoked_at=datetime.fromisoformat(row[11]) if row[11] else None,
    )


class SqliteSharedConsumptionRecordRepository(SharedConsumptionRecordRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, record: SharedConsumptionRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO shared_consumption_records
                (id, usage_record_id, grant_id, amount, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (record.id, record.usage_record_id, record.grant_id, record.amount, record.timestamp.isoformat()),
        )

    def list_by_usage_record(self, usage_record_id: str) -> list[SharedConsumptionRecord]:
        rows = self._conn.execute(
            """
            SELECT id, usage_record_id, grant_id, amount, timestamp
            FROM shared_consumption_records
            WHERE usage_record_id = ?
            """,
            (usage_record_id,),
        ).fetchall()
        return [_row_to_shared_consumption_record(row) for row in rows]

    def list_by_grant(self, grant_id: str) -> list[SharedConsumptionRecord]:
        rows = self._conn.execute(
            """
            SELECT id, usage_record_id, grant_id, amount, timestamp
            FROM shared_consumption_records
            WHERE grant_id = ?
            """,
            (grant_id,),
        ).fetchall()
        return [_row_to_shared_consumption_record(row) for row in rows]


def _row_to_shared_consumption_record(row: tuple) -> SharedConsumptionRecord:
    return SharedConsumptionRecord(
        id=row[0],
        usage_record_id=row[1],
        grant_id=row[2],
        amount=row[3],
        timestamp=datetime.fromisoformat(row[4]),
    )
