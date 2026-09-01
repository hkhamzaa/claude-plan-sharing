"""Pydantic request/response models for the Milestone 5 HTTP API.

These mirror the shapes already defined in `application/dto.py` and
`domain/models.py` field-for-field - they exist only so FastAPI can
validate/serialize JSON at the HTTP boundary; no business rule is
re-implemented or re-checked here (see `routes.py`'s module docstring).
Each `*_out` function is a small, explicit converter from an
application-layer dataclass to its HTTP schema - written out longhand
rather than via pydantic's `from_attributes` auto-mapping, so the mapping
stays easy to read and to keep in sync by eye as `dto.py` evolves.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from claude_share.application.dto import (
    ConsumeResult,
    EffectiveCapacity,
    MemberStatus,
    QuotaCheckResult,
    SharedDraw,
    WindowStatus,
)
from claude_share.domain.models import (
    CapacityGrant,
    CapacityRequest,
    CapacityType,
    Device,
    GrantStatus,
    Member,
    Pool,
    RequestStatus,
    WindowType,
)

# --- request bodies ---------------------------------------------------------


class CreatePoolRequest(BaseModel):
    name: str
    member_names: list[str]


class CheckQuotaRequest(BaseModel):
    member_id: str
    window_type: WindowType
    amount: int


class ConsumeRequest(BaseModel):
    member_id: str
    window_type: WindowType
    amount: int
    idempotency_key: str


class RequestCapacityRequest(BaseModel):
    pool_id: str
    requester_member_id: str
    target_member_id: str
    window_type: WindowType
    amount: int
    type: CapacityType
    message: str | None = None


class ApproveRequestBody(BaseModel):
    approving_member_id: str


class RejectRequestBody(BaseModel):
    rejecting_member_id: str


class RevokeGrantBody(BaseModel):
    revoking_member_id: str


class RegisterDeviceRequest(BaseModel):
    user_id: str
    device_name: str


# --- response bodies ---------------------------------------------------------


class PoolOut(BaseModel):
    id: str
    name: str
    member_count: int


class MemberOut(BaseModel):
    id: str
    pool_id: str
    user_id: str
    display_name: str


class CreatePoolResponse(BaseModel):
    pool: PoolOut
    members: list[MemberOut]


class QuotaCheckResultOut(BaseModel):
    member_id: str
    window_type: WindowType
    requested_amount: int
    allowed: bool
    allocation_units: int
    remaining_units: int


class SharedDrawOut(BaseModel):
    grant_id: str
    amount: int


class ConsumeResultOut(BaseModel):
    member_id: str
    window_type: WindowType
    amount: int
    idempotency_key: str
    accepted: bool
    replayed: bool
    allocation_units: int
    remaining_units: int
    shared_units_used: int
    shared_draws: list[SharedDrawOut]
    reason: str | None


class WindowStatusOut(BaseModel):
    window_type: WindowType
    allocation_units: int
    used_units: int
    remaining_units: int
    window_start: datetime
    reset_at: datetime


class MemberStatusOut(BaseModel):
    member_id: str
    pool_id: str
    display_name: str
    windows: dict[WindowType, WindowStatusOut]


class EffectiveCapacityOut(BaseModel):
    member_id: str
    window_type: WindowType
    base_allocation_units: int
    solid_sent: int
    solid_received: int
    guaranteed_units: int
    shared_offered: int
    shared_borrowed_potential: int
    potential_units: int


class CapacityRequestOut(BaseModel):
    id: str
    pool_id: str
    requester_member_id: str
    target_member_id: str
    window_type: WindowType
    amount: int
    type: CapacityType
    status: RequestStatus
    created_at: datetime
    approved_at: datetime | None
    expires_at: datetime | None
    message: str | None


class CapacityGrantOut(BaseModel):
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
    revoked_at: datetime | None


class DeviceOut(BaseModel):
    id: str
    user_id: str
    device_name: str
    created_at: datetime


class DeviceRegisteredOut(BaseModel):
    device: DeviceOut
    #: Plaintext bearer token - present in this ONE response only, right
    #: after registration. Save it now; the server cannot recover it later
    #: (only `Device.token_hash` is ever persisted).
    token: str


class MemberGrantsOut(BaseModel):
    sent: list[CapacityGrantOut]
    received: list[CapacityGrantOut]


class MemberPoolOverviewOut(BaseModel):
    member: MemberOut
    status: MemberStatusOut
    capacity: dict[WindowType, EffectiveCapacityOut]


class PoolOverviewOut(BaseModel):
    pool_id: str
    members: list[MemberPoolOverviewOut]


# --- dataclass -> schema converters ------------------------------------------


def pool_out(pool: Pool) -> PoolOut:
    return PoolOut(id=pool.id, name=pool.name, member_count=pool.member_count)


def member_out(member: Member) -> MemberOut:
    return MemberOut(id=member.id, pool_id=member.pool_id, user_id=member.user_id, display_name=member.display_name)


def quota_check_result_out(result: QuotaCheckResult) -> QuotaCheckResultOut:
    return QuotaCheckResultOut(
        member_id=result.member_id,
        window_type=result.window_type,
        requested_amount=result.requested_amount,
        allowed=result.allowed,
        allocation_units=result.allocation_units,
        remaining_units=result.remaining_units,
    )


def shared_draw_out(draw: SharedDraw) -> SharedDrawOut:
    return SharedDrawOut(grant_id=draw.grant_id, amount=draw.amount)


def consume_result_out(result: ConsumeResult) -> ConsumeResultOut:
    return ConsumeResultOut(
        member_id=result.member_id,
        window_type=result.window_type,
        amount=result.amount,
        idempotency_key=result.idempotency_key,
        accepted=result.accepted,
        replayed=result.replayed,
        allocation_units=result.allocation_units,
        remaining_units=result.remaining_units,
        shared_units_used=result.shared_units_used,
        shared_draws=[shared_draw_out(d) for d in result.shared_draws],
        reason=result.reason,
    )


def window_status_out(status: WindowStatus) -> WindowStatusOut:
    return WindowStatusOut(
        window_type=status.window_type,
        allocation_units=status.allocation_units,
        used_units=status.used_units,
        remaining_units=status.remaining_units,
        window_start=status.window_start,
        reset_at=status.reset_at,
    )


def member_status_out(status: MemberStatus) -> MemberStatusOut:
    return MemberStatusOut(
        member_id=status.member_id,
        pool_id=status.pool_id,
        display_name=status.display_name,
        windows={wt: window_status_out(ws) for wt, ws in status.windows.items()},
    )


def effective_capacity_out(capacity: EffectiveCapacity) -> EffectiveCapacityOut:
    return EffectiveCapacityOut(
        member_id=capacity.member_id,
        window_type=capacity.window_type,
        base_allocation_units=capacity.base_allocation_units,
        solid_sent=capacity.solid_sent,
        solid_received=capacity.solid_received,
        guaranteed_units=capacity.guaranteed_units,
        shared_offered=capacity.shared_offered,
        shared_borrowed_potential=capacity.shared_borrowed_potential,
        potential_units=capacity.potential_units,
    )


def capacity_request_out(request: CapacityRequest) -> CapacityRequestOut:
    return CapacityRequestOut(
        id=request.id,
        pool_id=request.pool_id,
        requester_member_id=request.requester_member_id,
        target_member_id=request.target_member_id,
        window_type=request.window_type,
        amount=request.amount,
        type=request.type,
        status=request.status,
        created_at=request.created_at,
        approved_at=request.approved_at,
        expires_at=request.expires_at,
        message=request.message,
    )


def capacity_grant_out(grant: CapacityGrant) -> CapacityGrantOut:
    return CapacityGrantOut(
        id=grant.id,
        pool_id=grant.pool_id,
        source_member_id=grant.source_member_id,
        recipient_member_id=grant.recipient_member_id,
        window_type=grant.window_type,
        amount=grant.amount,
        type=grant.type,
        status=grant.status,
        created_at=grant.created_at,
        activated_at=grant.activated_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
    )


def device_out(device: Device) -> DeviceOut:
    return DeviceOut(id=device.id, user_id=device.user_id, device_name=device.device_name, created_at=device.created_at)
