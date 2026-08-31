"""Shared read-side helpers for grant-derived capacity figures (Milestone 2).

Both `QuotaService.consume()` (to enforce the guaranteed-capacity ceiling
and to draw from SHARED grants) and `CapacityService` (to evaluate
`approve_request()`'s over-commitment checks and to answer
`get_effective_capacity()`) need the same answer to "what grants currently
apply to this member, in this window, and how much do they add up to?".
Keeping that logic in one place means the two services can't silently
drift on what counts as "active".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from claude_share.domain.capacity import is_grant_usable
from claude_share.domain.models import CapacityGrant, CapacityType, GrantStatus, WindowType
from claude_share.domain.repository import UnitOfWork


@dataclass(frozen=True, slots=True)
class GrantSummary:
    solid_sent: int
    solid_received: int
    shared_offered: int
    #: Ceiling sum of active SHARED grants held as recipient (see
    #: EffectiveCapacity.shared_borrowed_potential for why this is a ceiling,
    #: not a guarantee, and is not reduced by amounts already drawn).
    shared_borrowed_potential: int
    #: Active SHARED grants held as recipient, oldest-created first - the
    #: draw order consume() uses when covering a shortfall.
    shared_grants_as_recipient: tuple[CapacityGrant, ...]


def active_grants_as_of(uow: UnitOfWork, grants: list[CapacityGrant], now: datetime) -> list[CapacityGrant]:
    """Filter to grants usable right now, lazily expiring any that are stale.

    A grant whose status is still ACTIVE but whose `expires_at` has passed
    is persisted as EXPIRED as a side effect (see docs/architecture.md:
    "Grant expiration policy" - lazy expiration on access, no background
    sweep in this milestone), then excluded from the result.
    """
    usable = []
    for grant in grants:
        if grant.status is GrantStatus.ACTIVE and not is_grant_usable(grant, now):
            uow.grants.update(replace(grant, status=GrantStatus.EXPIRED))
            continue
        if grant.status is GrantStatus.ACTIVE:
            usable.append(grant)
    return usable


def member_grant_summary(uow: UnitOfWork, member_id: str, window_type: WindowType, now: datetime) -> GrantSummary:
    as_source = active_grants_as_of(uow, uow.grants.list_by_source(member_id, window_type), now)
    as_recipient = active_grants_as_of(uow, uow.grants.list_by_recipient(member_id, window_type), now)

    solid_sent = sum(g.amount for g in as_source if g.type is CapacityType.SOLID)
    shared_offered = sum(g.amount for g in as_source if g.type is CapacityType.SHARED)
    solid_received = sum(g.amount for g in as_recipient if g.type is CapacityType.SOLID)

    shared_as_recipient = sorted(
        (g for g in as_recipient if g.type is CapacityType.SHARED),
        key=lambda g: g.created_at,
    )
    shared_borrowed_potential = sum(g.amount for g in shared_as_recipient)

    return GrantSummary(
        solid_sent=solid_sent,
        solid_received=solid_received,
        shared_offered=shared_offered,
        shared_borrowed_potential=shared_borrowed_potential,
        shared_grants_as_recipient=tuple(shared_as_recipient),
    )


def grant_lifetime_drawn(uow: UnitOfWork, grant_id: str) -> int:
    """Total already consumed against a specific SHARED grant, across all
    past consume() calls. A grant's `amount` is a lifetime ceiling for that
    grant, not a per-call one - see docs/architecture.md."""
    return sum(record.amount for record in uow.shared_consumption_records.list_by_grant(grant_id))
