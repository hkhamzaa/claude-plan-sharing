"""Application service implementing the Milestone 1 use cases, with
Milestone 2's grant-aware `consume()`.

QuotaService depends only on the domain layer's `UnitOfWork` abstraction
(dependency injected as a zero-argument factory), never on a concrete
persistence technology. This is what lets the same service run against the
SQLite implementation today and, e.g., an in-memory or Postgres
implementation later without any change here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from claude_share.application.capacity_queries import grant_lifetime_drawn, member_grant_summary
from claude_share.application.dto import (
    ConsumeResult,
    MemberStatus,
    QuotaCheckResult,
    SharedDraw,
    WindowStatus,
)
from claude_share.application.ids import new_id
from claude_share.domain.allocation import compute_equal_allocations_bps
from claude_share.domain.capacity import compute_guaranteed_units
from claude_share.domain.errors import (
    IdempotencyConflictError,
    MemberNotFoundError,
    QuotaWindowNotFoundError,
)
from claude_share.domain.models import (
    Allocation,
    CapacityGrant,
    Member,
    Pool,
    QuotaWindow,
    SharedConsumptionRecord,
    UsageRecord,
    WindowType,
)
from claude_share.domain.repository import UnitOfWork

_ALL_WINDOW_TYPES = (WindowType.FIVE_HOUR, WindowType.WEEKLY)


class QuotaService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def create_pool(self, name: str, member_names: list[str]) -> Pool:
        """Create a pool and N members with an equal quota split.

        Allocation is computed once (as basis points) and used as the
        starting allocation for both the FIVE_HOUR and WEEKLY windows of
        every member. See docs/architecture.md for the "bps as units"
        assumption.
        """
        if not member_names:
            raise ValueError("member_names must contain at least one name")

        shares_bps = compute_equal_allocations_bps(len(member_names))

        pool = Pool(id=new_id(), name=name, member_count=len(member_names))
        now = datetime.now(timezone.utc)

        members: list[Member] = []
        allocations: list[Allocation] = []
        windows: list[QuotaWindow] = []

        for display_name, bps in zip(member_names, shares_bps, strict=True):
            member = Member(id=new_id(), pool_id=pool.id, user_id=new_id(), display_name=display_name)
            members.append(member)
            allocations.append(Allocation(member_id=member.id, bps=bps))
            for window_type in _ALL_WINDOW_TYPES:
                windows.append(
                    QuotaWindow(
                        member_id=member.id,
                        window_type=window_type,
                        window_start=now,
                        reset_at=now + window_type.duration,
                        allocation_units=bps,
                        usage_units=0,
                    )
                )

        with self._uow_factory() as uow:
            uow.pools.add(pool)
            for member in members:
                uow.members.add(member)
            for allocation in allocations:
                uow.allocations.add(allocation)
            for window in windows:
                uow.windows.add(window)
            uow.commit()

        return pool

    def list_members(self, pool_id: str) -> list[Member]:
        """Read-only helper so front-ends can discover member ids after creation."""
        with self._uow_factory() as uow:
            members = uow.members.list_by_pool(pool_id)
            uow.commit()
        return members

    def check_quota(self, member_id: str, window_type: WindowType, amount: int) -> QuotaCheckResult:
        """Read-only check: could `amount` more units be consumed right now?

        This checks the member's own base allocation only (Milestone 1
        semantics), not grant-adjusted guaranteed/shared capacity - use
        `CapacityService.get_effective_capacity()` for the grant-aware view.
        """
        if amount <= 0:
            raise ValueError("amount must be a positive integer")

        with self._uow_factory() as uow:
            window = uow.windows.get(member_id, window_type)
            if window is None:
                uow.rollback()
                raise QuotaWindowNotFoundError(member_id, window_type.value)
            allowed = window.can_consume(amount)
            uow.commit()

        return QuotaCheckResult(
            member_id=member_id,
            window_type=window_type,
            requested_amount=amount,
            allowed=allowed,
            allocation_units=window.allocation_units,
            remaining_units=window.remaining_units,
        )

    def consume(
        self,
        member_id: str,
        window_type: WindowType,
        amount: int,
        idempotency_key: str,
    ) -> ConsumeResult:
        """Atomically consume `amount` units, or reject without side effects.

        The admission ceiling is the member's GUARANTEED capacity (base
        allocation, adjusted for active SOLID grants sent/received), not raw
        base allocation. If guaranteed capacity alone can't cover `amount`,
        the shortfall is drawn from the member's active SHARED grants
        (oldest first), each capped by its source's own live remaining
        guaranteed balance at this exact moment - owner priority - and by
        that grant's own lifetime-drawn ceiling. See docs/architecture.md
        ("SOLID vs SHARED accounting", "Owner priority mechanics").

        Everything here - the idempotency check, every guaranteed/shared
        balance read, and every write - happens inside one SQLite
        BEGIN IMMEDIATE transaction, so no other consume(), approve_request(),
        or revoke_grant() call can interleave and invalidate a balance this
        call already checked. A rejection leaves stored state unchanged.
        """
        if amount <= 0:
            raise ValueError("amount must be a positive integer")
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")

        now = datetime.now(timezone.utc)

        with self._uow_factory() as uow:
            existing = uow.usage_records.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.member_id != member_id
                    or existing.window_type != window_type
                    or existing.amount != amount
                ):
                    uow.rollback()
                    raise IdempotencyConflictError(idempotency_key)

                window = uow.windows.get(member_id, window_type)
                if window is None:
                    uow.rollback()
                    raise QuotaWindowNotFoundError(member_id, window_type.value)
                summary = member_grant_summary(uow, member_id, window_type, now)
                guaranteed = compute_guaranteed_units(
                    window.allocation_units, summary.solid_sent, summary.solid_received
                )
                shared_records = uow.shared_consumption_records.list_by_usage_record(existing.id)
                uow.commit()
                return ConsumeResult(
                    member_id=member_id,
                    window_type=window_type,
                    amount=amount,
                    idempotency_key=idempotency_key,
                    accepted=True,
                    replayed=True,
                    allocation_units=guaranteed,
                    remaining_units=max(guaranteed - window.usage_units, 0),
                    shared_units_used=sum(r.amount for r in shared_records),
                    shared_draws=tuple(SharedDraw(r.grant_id, r.amount) for r in shared_records),
                )

            member = uow.members.get(member_id)
            if member is None:
                uow.rollback()
                raise MemberNotFoundError(member_id)

            window = uow.windows.get(member_id, window_type)
            if window is None:
                uow.rollback()
                raise QuotaWindowNotFoundError(member_id, window_type.value)

            summary = member_grant_summary(uow, member_id, window_type, now)
            guaranteed_units = compute_guaranteed_units(
                window.allocation_units, summary.solid_sent, summary.solid_received
            )
            own_available = max(guaranteed_units - window.usage_units, 0)
            own_portion = min(amount, own_available)
            shortfall = amount - own_portion

            window_updates: dict[tuple[str, WindowType], QuotaWindow] = {(member_id, window_type): window}
            draws: list[tuple[CapacityGrant, int]] = []

            if shortfall > 0:
                for grant in summary.shared_grants_as_recipient:
                    if shortfall <= 0:
                        break

                    source_key = (grant.source_member_id, window_type)
                    if source_key not in window_updates:
                        source_window = uow.windows.get(*source_key)
                        if source_window is None:
                            continue
                        window_updates[source_key] = source_window
                    source_window = window_updates[source_key]

                    source_summary = member_grant_summary(uow, grant.source_member_id, window_type, now)
                    source_guaranteed = compute_guaranteed_units(
                        source_window.allocation_units, source_summary.solid_sent, source_summary.solid_received
                    )
                    source_available = max(source_guaranteed - source_window.usage_units, 0)
                    grant_remaining = grant.amount - grant_lifetime_drawn(uow, grant.id)

                    draw_amount = min(grant_remaining, source_available, shortfall)
                    if draw_amount <= 0:
                        continue

                    draws.append((grant, draw_amount))
                    window_updates[source_key] = replace(
                        source_window, usage_units=source_window.usage_units + draw_amount
                    )
                    shortfall -= draw_amount

            if shortfall > 0:
                uow.rollback()
                return ConsumeResult(
                    member_id=member_id,
                    window_type=window_type,
                    amount=amount,
                    idempotency_key=idempotency_key,
                    accepted=False,
                    replayed=False,
                    allocation_units=guaranteed_units,
                    remaining_units=own_available,
                    reason="insufficient_quota",
                )

            recipient_key = (member_id, window_type)
            recipient_window = window_updates[recipient_key]
            window_updates[recipient_key] = replace(
                recipient_window, usage_units=recipient_window.usage_units + own_portion
            )

            for window_to_persist in window_updates.values():
                uow.windows.update(window_to_persist)

            usage_record = UsageRecord(
                id=new_id(),
                member_id=member_id,
                window_type=window_type,
                amount=amount,
                idempotency_key=idempotency_key,
                timestamp=now,
            )
            uow.usage_records.add(usage_record)
            for grant, draw_amount in draws:
                uow.shared_consumption_records.add(
                    SharedConsumptionRecord(
                        id=new_id(),
                        usage_record_id=usage_record.id,
                        grant_id=grant.id,
                        amount=draw_amount,
                        timestamp=now,
                    )
                )

            uow.commit()

            final_recipient_window = window_updates[recipient_key]
            return ConsumeResult(
                member_id=member_id,
                window_type=window_type,
                amount=amount,
                idempotency_key=idempotency_key,
                accepted=True,
                replayed=False,
                allocation_units=guaranteed_units,
                remaining_units=max(guaranteed_units - final_recipient_window.usage_units, 0),
                shared_units_used=sum(d for _, d in draws),
                shared_draws=tuple(SharedDraw(g.id, d) for g, d in draws if d > 0),
            )

    def get_status(self, member_id: str) -> MemberStatus:
        """Base-allocation bookkeeping only (unchanged from Milestone 1).

        Grant-adjusted figures live in
        `CapacityService.get_effective_capacity()` instead - see
        docs/architecture.md for why the two are kept separate.
        """
        with self._uow_factory() as uow:
            member = uow.members.get(member_id)
            if member is None:
                uow.rollback()
                raise MemberNotFoundError(member_id)
            windows = uow.windows.list_by_member(member_id)
            uow.commit()

        window_map = {
            window.window_type: WindowStatus(
                window_type=window.window_type,
                allocation_units=window.allocation_units,
                used_units=window.usage_units,
                remaining_units=window.remaining_units,
                window_start=window.window_start,
                reset_at=window.reset_at,
            )
            for window in windows
        }

        return MemberStatus(
            member_id=member.id,
            pool_id=member.pool_id,
            display_name=member.display_name,
            windows=window_map,
        )
