"""HTTP routes for the Milestone 5 central server.

One endpoint per existing application-layer method, at the granularity the
milestone brief specified - this module is a thin adapter and nothing more:
every handler except the two bootstrap exceptions (`create_pool`,
`register_device` - see `server/auth.py`'s module docstring) (1) authenticates and, where relevant, checks that the
authenticated device's user_id actually owns the member_id it's about to
act as (`server/auth.py:require_member()`), (2) calls straight through to
`QuotaService`/`CapacityService`/`AgentService`, and (3) converts the
returned dataclass to its `schemas.py` response model. No business
validation, quota arithmetic, or capacity-delegation rule is duplicated or
re-implemented here - see docs/architecture.md for why that separation
matters. `DomainError`s raised by a service are not caught here; a FastAPI
exception handler registered in `app.py` (using `server/errors.py`'s
mapping) turns them into the right HTTP status code.

## Which member_id each endpoint checks ownership of

Several endpoints reference *two* member_ids (e.g. `request_capacity`'s
`requester_member_id` and `target_member_id`). Only the member_id
representing *the caller themselves* is checked against the authenticated
device (`require_member`) - the other is just an ordinary reference to
some other pool member, validated the same way it always was by the
application/domain layers (must exist, must belong to the same pool,
etc.), exactly as for a local CLI caller who can address any member_id
they know. Concretely: `get_status`/`quota/check`/`quota/consume`/
`get_effective_capacity` check the `member_id` they operate on;
`request_capacity` checks `requester_member_id` (the person asking, not
the owner being asked); `approve_request`/`reject_request`/`revoke_grant`
check the member_id passed as the approving/rejecting/revoking party
(which the application layer separately re-validates actually matches
`target_member_id`/`source_member_id` - a device could otherwise claim to
approve on behalf of a member_id it owns that isn't the request's actual
target, and `CapacityService.approve_request()` already rejects that with
`NotAuthorizedError`).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from claude_share.application.agent_service import AgentService
from claude_share.application.capacity_service import CapacityService
from claude_share.application.quota_service import QuotaService
from claude_share.domain.models import Device, WindowType
from claude_share.domain.repository import UnitOfWork
from claude_share.server.auth import get_agent_service, get_current_device, get_uow_factory, require_member
from claude_share.server.schemas import (
    ApproveRequestBody,
    CapacityGrantOut,
    CapacityRequestOut,
    CheckQuotaRequest,
    ConsumeRequest,
    ConsumeResultOut,
    CreatePoolRequest,
    CreatePoolResponse,
    DeviceOut,
    DeviceRegisteredOut,
    EffectiveCapacityOut,
    MemberOut,
    MemberStatusOut,
    QuotaCheckResultOut,
    RegisterDeviceRequest,
    RejectRequestBody,
    RequestCapacityRequest,
    RevokeGrantBody,
    capacity_grant_out,
    capacity_request_out,
    consume_result_out,
    device_out,
    effective_capacity_out,
    member_out,
    member_status_out,
    pool_out,
    quota_check_result_out,
)

router = APIRouter()


def get_quota_service(request: Request) -> QuotaService:
    return request.app.state.quota_service


def get_capacity_service(request: Request) -> CapacityService:
    return request.app.state.capacity_service


# --- pools / members ---------------------------------------------------------


@router.post("/pools", response_model=CreatePoolResponse, status_code=201)
def create_pool(
    body: CreatePoolRequest,
    quota_service: QuotaService = Depends(get_quota_service),
) -> CreatePoolResponse:
    """No auth required - see server/auth.py's module docstring for why
    pool creation is one of the two bootstrap exceptions."""
    pool = quota_service.create_pool(body.name, body.member_names)
    members = quota_service.list_members(pool.id)
    return CreatePoolResponse(pool=pool_out(pool), members=[member_out(m) for m in members])


@router.get("/pools/{pool_id}/members", response_model=list[MemberOut])
def list_members(
    pool_id: str,
    device: Device = Depends(get_current_device),
    quota_service: QuotaService = Depends(get_quota_service),
) -> list[MemberOut]:
    """Requires a valid bearer token like every other endpoint. Pool creation
    returns the full member list in its response body, so callers never
    need an unauthenticated listing immediately after `POST /pools`."""
    _ = device  # authenticated; no member_id ownership check on this read-only pool directory
    members = quota_service.list_members(pool_id)
    return [member_out(m) for m in members]


# --- quota ---------------------------------------------------------------


@router.get("/members/{member_id}/status", response_model=MemberStatusOut)
def get_status(
    member_id: str,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    quota_service: QuotaService = Depends(get_quota_service),
) -> MemberStatusOut:
    require_member(uow_factory, device, member_id)
    return member_status_out(quota_service.get_status(member_id))


@router.post("/quota/check", response_model=QuotaCheckResultOut)
def check_quota(
    body: CheckQuotaRequest,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    quota_service: QuotaService = Depends(get_quota_service),
) -> QuotaCheckResultOut:
    require_member(uow_factory, device, body.member_id)
    result = quota_service.check_quota(body.member_id, body.window_type, body.amount)
    return quota_check_result_out(result)


@router.post("/quota/consume", response_model=ConsumeResultOut)
def consume(
    body: ConsumeRequest,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    quota_service: QuotaService = Depends(get_quota_service),
) -> ConsumeResultOut:
    require_member(uow_factory, device, body.member_id)
    result = quota_service.consume(body.member_id, body.window_type, body.amount, body.idempotency_key)
    return consume_result_out(result)


# --- capacity delegation ---------------------------------------------------


@router.post("/capacity/requests", response_model=CapacityRequestOut, status_code=201)
def request_capacity(
    body: RequestCapacityRequest,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    capacity_service: CapacityService = Depends(get_capacity_service),
) -> CapacityRequestOut:
    # The caller is the requester (the one asking); target_member_id is
    # just another member reference, validated by the application layer.
    require_member(uow_factory, device, body.requester_member_id)
    request = capacity_service.request_capacity(
        pool_id=body.pool_id,
        requester_member_id=body.requester_member_id,
        target_member_id=body.target_member_id,
        window_type=body.window_type,
        amount=body.amount,
        type=body.type,
        message=body.message,
    )
    return capacity_request_out(request)


@router.post("/capacity/requests/{request_id}/approve", response_model=CapacityGrantOut)
def approve_request(
    request_id: str,
    body: ApproveRequestBody,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    capacity_service: CapacityService = Depends(get_capacity_service),
) -> CapacityGrantOut:
    require_member(uow_factory, device, body.approving_member_id)
    grant = capacity_service.approve_request(request_id, body.approving_member_id)
    return capacity_grant_out(grant)


@router.post("/capacity/requests/{request_id}/reject", response_model=CapacityRequestOut)
def reject_request(
    request_id: str,
    body: RejectRequestBody,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    capacity_service: CapacityService = Depends(get_capacity_service),
) -> CapacityRequestOut:
    require_member(uow_factory, device, body.rejecting_member_id)
    request = capacity_service.reject_request(request_id, body.rejecting_member_id)
    return capacity_request_out(request)


@router.post("/capacity/grants/{grant_id}/revoke", response_model=CapacityGrantOut)
def revoke_grant(
    grant_id: str,
    body: RevokeGrantBody,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    capacity_service: CapacityService = Depends(get_capacity_service),
) -> CapacityGrantOut:
    require_member(uow_factory, device, body.revoking_member_id)
    grant = capacity_service.revoke_grant(grant_id, body.revoking_member_id)
    return capacity_grant_out(grant)


@router.get("/members/{member_id}/capacity", response_model=EffectiveCapacityOut)
def get_effective_capacity(
    member_id: str,
    window: WindowType,
    device: Device = Depends(get_current_device),
    uow_factory: Callable[[], UnitOfWork] = Depends(get_uow_factory),
    capacity_service: CapacityService = Depends(get_capacity_service),
) -> EffectiveCapacityOut:
    require_member(uow_factory, device, member_id)
    capacity = capacity_service.get_effective_capacity(member_id, window)
    return effective_capacity_out(capacity)


# --- devices / agent identity ------------------------------------------------


@router.post("/devices", response_model=DeviceRegisteredOut, status_code=201)
def register_device(
    body: RegisterDeviceRequest,
    agent_service: AgentService = Depends(get_agent_service),
) -> DeviceRegisteredOut:
    """No auth required - see server/auth.py's module docstring for why
    device registration is the other bootstrap exception. This is the only
    response that ever carries the plaintext device token."""
    device = agent_service.register_device(body.user_id, body.device_name)
    assert device.token is not None  # always set on a freshly-registered Device
    return DeviceRegisteredOut(device=device_out(device), token=device.token)


@router.get("/users/{user_id}/devices", response_model=list[DeviceOut])
def list_devices(
    user_id: str,
    device: Device = Depends(get_current_device),
    agent_service: AgentService = Depends(get_agent_service),
) -> list[DeviceOut]:
    if device.user_id != user_id:
        raise HTTPException(status_code=403, detail="Cannot list another user's devices.")
    return [device_out(d) for d in agent_service.list_devices(user_id)]
