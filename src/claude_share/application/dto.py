"""Data-transfer objects returned by the application layer.

These are plain, immutable, serialisation-friendly shapes consumed by the
CLI (and, later, other front-ends). They are distinct from domain entities
so that domain models can evolve without breaking the public application API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from claude_share.domain.models import WindowType


@dataclass(frozen=True, slots=True)
class QuotaCheckResult:
    member_id: str
    window_type: WindowType
    requested_amount: int
    allowed: bool
    allocation_units: int
    remaining_units: int


@dataclass(frozen=True, slots=True)
class SharedDraw:
    """One SHARED grant's contribution toward covering a consume() call."""

    grant_id: str
    amount: int


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    member_id: str
    window_type: WindowType
    amount: int
    idempotency_key: str
    accepted: bool
    replayed: bool
    #: The member's own guaranteed ceiling at the time of this call (base
    #: allocation, adjusted for active SOLID grants). Equal to the member's
    #: base allocation when no SOLID grants apply - see Milestone 1 behavior.
    allocation_units: int
    #: The member's own guaranteed remaining balance after this call (not
    #: reduced by any SHARED amount drawn on their behalf - that capacity
    #: was never theirs to begin with; see `shared_units_used`).
    remaining_units: int
    #: Portion of `amount` (if accepted) that had to be drawn from SHARED
    #: grants because the member's own guaranteed capacity fell short.
    shared_units_used: int = 0
    shared_draws: tuple[SharedDraw, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class WindowStatus:
    window_type: WindowType
    allocation_units: int
    used_units: int
    remaining_units: int
    window_start: datetime
    reset_at: datetime


@dataclass(frozen=True, slots=True)
class MemberStatus:
    member_id: str
    pool_id: str
    display_name: str
    windows: dict[WindowType, WindowStatus]


@dataclass(frozen=True, slots=True)
class EffectiveCapacity:
    """Grant-aware view of a member's capacity in one window.

    `guaranteed_units` is a real ceiling: consume() enforces it directly.
    `potential_units` is an upper bound, not a promise - see
    `shared_borrowed_potential`'s docstring below and docs/architecture.md.
    """

    member_id: str
    window_type: WindowType
    base_allocation_units: int
    solid_sent: int
    solid_received: int
    guaranteed_units: int
    shared_offered: int
    #: Ceiling sum of active SHARED grants held as recipient. NOT a
    #: guarantee: how much is actually drawable at consume() time depends on
    #: each source's own usage at that moment, and is not reduced here by
    #: amounts already drawn historically against those grants.
    shared_borrowed_potential: int
    #: guaranteed_units + shared_borrowed_potential. An upper bound on what
    #: might be consumable right now, not a guaranteed balance.
    potential_units: int
