"""Core domain entities and value objects for the quota engine.

Everything in this module is a plain, immutable value object. There is no
persistence, no Claude-specific vocabulary (tokens, prompts, models), and no
SQLite-specific vocabulary (rows, cursors, connections). See
docs/architecture.md for why that separation matters.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from claude_share.domain.errors import InsufficientQuotaError

#: 100% of a pool's quota, expressed in fixed-point basis points.
TOTAL_ALLOCATION_BPS = 10_000

#: Placeholder window durations (see docs/architecture.md: "fixed-duration
#: window" assumption). A future UsageWindowProvider will replace these with
#: real Claude usage-window boundaries.
FIVE_HOUR_WINDOW_DURATION = timedelta(hours=5)
WEEKLY_WINDOW_DURATION = timedelta(days=7)


class WindowType(str, Enum):
    """The two quota windows that are tracked independently per member."""

    FIVE_HOUR = "five_hour"
    WEEKLY = "weekly"

    @property
    def duration(self) -> timedelta:
        if self is WindowType.FIVE_HOUR:
            return FIVE_HOUR_WINDOW_DURATION
        return WEEKLY_WINDOW_DURATION


@dataclass(frozen=True, slots=True)
class Pool:
    """A group of trusted members sharing 100% of a subscription's quota."""

    id: str
    name: str
    member_count: int


@dataclass(frozen=True, slots=True)
class Member:
    """A single participant in a pool."""

    id: str
    pool_id: str
    user_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class Allocation:
    """A member's fixed base quota share, in basis points of the pool total.

    One Allocation exists per member. Basis points are apportioned equally
    across all members of a pool using deterministic rounding, and the sum
    across all members of a pool is guaranteed to equal TOTAL_ALLOCATION_BPS.
    """

    member_id: str
    bps: int

    def __post_init__(self) -> None:
        if not 0 <= self.bps <= TOTAL_ALLOCATION_BPS:
            raise ValueError(f"bps must be within [0, {TOTAL_ALLOCATION_BPS}], got {self.bps}")


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    """A member's usage window (FIVE_HOUR or WEEKLY), tracked independently.

    `allocation_units` is the member's BASE capacity for this window,
    expressed in abstract quota units (see docs/architecture.md: "bps as
    units" assumption) - it is never mutated by a Milestone 2 grant.
    `usage_units` is monotonically non-decreasing consumption within the
    window, and this is deliberately not validated against
    `allocation_units` here: a member who has received SOLID capacity from
    another member (Milestone 2) can legitimately accumulate `usage_units`
    beyond their own `allocation_units`, since their real ceiling
    (`guaranteed_units`, computed in the application layer from active
    grants) can exceed it. See docs/architecture.md ("Base allocation vs.
    guaranteed vs. potential capacity").
    """

    member_id: str
    window_type: WindowType
    window_start: datetime
    reset_at: datetime
    allocation_units: int
    usage_units: int = 0

    def __post_init__(self) -> None:
        if self.allocation_units < 0:
            raise ValueError("allocation_units must not be negative")
        if self.usage_units < 0:
            raise ValueError("usage_units must not be negative")

    @property
    def remaining_units(self) -> int:
        return self.allocation_units - self.usage_units

    def can_consume(self, amount: int) -> bool:
        if amount <= 0:
            raise ValueError("amount must be a positive integer")
        return amount <= self.remaining_units

    def consume(self, amount: int) -> QuotaWindow:
        """Return a new QuotaWindow with `amount` deducted.

        Raises InsufficientQuotaError rather than allowing usage_units to
        exceed allocation_units. This method has no side effects; callers
        (the application layer) are responsible for persisting the result
        atomically.
        """
        if amount <= 0:
            raise ValueError("amount must be a positive integer")
        if amount > self.remaining_units:
            raise InsufficientQuotaError(
                member_id=self.member_id,
                window_type=self.window_type.value,
                requested=amount,
                remaining=self.remaining_units,
            )
        return replace(self, usage_units=self.usage_units + amount)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """An immutable, idempotent record of a single unit-consumption event."""

    id: str
    member_id: str
    window_type: WindowType
    amount: int
    idempotency_key: str
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be a positive integer")


class CapacityType(str, Enum):
    """The two capacity-delegation primitives (Milestone 2)."""

    #: Permanent-until-revoked transfer: source's guaranteed capacity
    #: decreases, recipient's increases, by the same amount.
    SOLID = "solid"

    #: Conditional, revocable access to the source's currently-unused
    #: capacity. Nothing is transferred; the source keeps priority.
    SHARED = "shared"


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class GrantStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class CapacityRequest:
    """A member's ask to receive SOLID or SHARED capacity from another member.

    `requester_member_id` is the member who would receive the capacity;
    `target_member_id` is the member who owns it and must approve or reject.
    Creating a request never moves or reserves capacity - see
    `docs/architecture.md` for the full request -> approval -> grant
    lifecycle.
    """

    id: str
    pool_id: str
    requester_member_id: str
    target_member_id: str
    window_type: WindowType
    amount: int
    type: CapacityType
    status: RequestStatus
    created_at: datetime
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be a positive integer")
        if self.requester_member_id == self.target_member_id:
            raise ValueError("requester_member_id and target_member_id must differ")


@dataclass(frozen=True, slots=True)
class CapacityGrant:
    """An active (or formerly active) capacity delegation created by approving a request."""

    id: str
    pool_id: str
    source_member_id: str
    recipient_member_id: str
    window_type: WindowType
    amount: int
    type: CapacityType
    status: GrantStatus
    created_at: datetime
    activated_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be a positive integer")
        if self.source_member_id == self.recipient_member_id:
            raise ValueError("source_member_id and recipient_member_id must differ")


@dataclass(frozen=True, slots=True)
class Device:
    """A machine registered as acting on behalf of a user_id (Milestone 3).

    Bookkeeping/identity only: it does not change quota math, which is
    still keyed purely by member_id exactly as in Milestones 1-2. Multiple
    devices under the same user_id are expected to `join_pool` as the same
    member_id, which already resolves to one shared quota ledger - this
    model just makes that identity explicit and persistent instead of
    requiring member_id to be passed on every call.

    `token_hash` (Milestone 5) is the only credential ever persisted for
    this device - a SHA-256 digest of an opaque bearer token minted once at
    registration. `token` carries the corresponding *plaintext* token, but
    only transiently: it is populated on the object `AgentService.register_device()`
    returns right after creation, is never written to storage, and is
    always `None` on a `Device` loaded back via `get()`/`list_by_user()`.
    See docs/architecture.md ("Milestone 5 - device auth tokens").
    """

    id: str
    user_id: str
    device_name: str
    created_at: datetime
    token_hash: str
    token: str | None = None


@dataclass(frozen=True, slots=True)
class SharedConsumptionRecord:
    """Audit trail entry: records that a consume() call drew `amount` units
    from a specific SHARED grant to help satisfy the recipient's request.

    Linked to the UsageRecord created for the recipient's consume() call
    (`usage_record_id`) and to the CapacityGrant it drew from (`grant_id`).
    A single consume() call may produce zero, one, or several of these if it
    draws from more than one SHARED grant to cover its shortfall.
    """

    id: str
    usage_record_id: str
    grant_id: str
    amount: int
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be a positive integer")
