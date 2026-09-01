"""Milestone 5: the remote (server-connected) counterpart to
`QuotaService`/`CapacityService`/`AgentService`, talking HTTP instead of a
local `UnitOfWork`.

## Why this is a separate HTTP client path, not a UnitOfWork adapter

`UnitOfWork` is a transaction boundary over *repositories* - it exists so
`QuotaService.consume()` can do "read a QuotaWindow row, maybe read some
CapacityGrant rows, write updated rows, all atomically" without knowing
whether that's SQLite or Postgres underneath. An HTTP call to
`POST /quota/consume` isn't shaped like that at all: it's a single
already-composed request/response round trip to a REMOTE copy of the
*entire* `consume()` method, not a sequence of individual repository
reads/writes this process could wrap in its own transaction. Trying to
make `RemoteQuotaService` implement `UnitOfWork`/the repository ports would
mean inventing fake repository methods that don't actually do row-level
I/O (each one would have to either make its own separate HTTP call,
defeating the point of atomicity, or buffer state client-side and
guess at a batching scheme the server doesn't support) - it doesn't
"stretch" cleanly, it would actively misrepresent what's happening over
the wire. So instead, `RemoteQuotaService`/`RemoteCapacityService`/
`RemoteAgentService` below duck-type `QuotaService`/`CapacityService`/
`AgentService`'s own *public methods* directly (`create_pool`, `consume`,
`get_effective_capacity`, ...), each one being exactly one HTTP call, and
return the exact same `application/dto.py`/`domain/models.py` dataclasses
those local services return. That's what lets `cli/main.py`'s existing
`_cmd_*` functions and `agent/commands.py:agent_status()` work completely
unchanged against either mode - they only ever call methods on "a
QuotaService-shaped object," never construct a `UnitOfWork` themselves.
`cli/main.py` is the one place that decides, once, up front, whether a
given identity means "build local services around SqliteUnitOfWork" or
"build these remote services around an httpx.Client" - see that module's
`_build_services()`. Nothing downstream of that choice needs to know which
mode it's in.

## Error handling

Any non-2xx response raises `agent.errors.RemoteRequestError` (see that
class's docstring for why it isn't split by status code) - so
`cli/main.py:main()`'s existing `except (DomainError, AgentError)` handler
already reports it exactly the way it reports a local `DomainError`,
without cli/main.py needing to know or care that the error actually came
from a server 400 lines away.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from claude_share.agent.errors import RemoteRequestError
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

DEFAULT_TIMEOUT_SECONDS = 10.0


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise RemoteRequestError(response.status_code, str(detail))


class _RemoteBase:
    """Shared HTTP plumbing for the three remote service classes below."""

    def __init__(
        self,
        server_url: str,
        device_token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=server_url, timeout=DEFAULT_TIMEOUT_SECONDS)
        self._device_token = device_token

    def _headers(self) -> dict[str, str]:
        if self._device_token is None:
            return {}
        return {"Authorization": f"Bearer {self._device_token}"}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, path, headers=self._headers(), **kwargs)
        _raise_for_status(response)
        return response

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# --- QuotaService --------------------------------------------------------


class RemoteQuotaService(_RemoteBase):
    """HTTP-backed counterpart to `application.quota_service.QuotaService`."""

    def __init__(
        self,
        server_url: str,
        device_token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(server_url, device_token, client)
        self.last_created_members: list[Member] | None = None

    def create_pool(self, name: str, member_names: list[str]) -> Pool:
        body = self._request("POST", "/pools", json={"name": name, "member_names": member_names}).json()
        self.last_created_members = [_member_from_json(m) for m in body["members"]]
        return _pool_from_json(body["pool"])

    def list_members(self, pool_id: str) -> list[Member]:
        body = self._request("GET", f"/pools/{pool_id}/members").json()
        return [_member_from_json(m) for m in body]

    def get_status(self, member_id: str) -> MemberStatus:
        body = self._request("GET", f"/members/{member_id}/status").json()
        return _member_status_from_json(body)

    def check_quota(self, member_id: str, window_type: WindowType, amount: int) -> QuotaCheckResult:
        body = self._request(
            "POST",
            "/quota/check",
            json={"member_id": member_id, "window_type": window_type.value, "amount": amount},
        ).json()
        return QuotaCheckResult(
            member_id=body["member_id"],
            window_type=WindowType(body["window_type"]),
            requested_amount=body["requested_amount"],
            allowed=body["allowed"],
            allocation_units=body["allocation_units"],
            remaining_units=body["remaining_units"],
        )

    def consume(
        self, member_id: str, window_type: WindowType, amount: int, idempotency_key: str
    ) -> ConsumeResult:
        body = self._request(
            "POST",
            "/quota/consume",
            json={
                "member_id": member_id,
                "window_type": window_type.value,
                "amount": amount,
                "idempotency_key": idempotency_key,
            },
        ).json()
        return _consume_result_from_json(body)


# --- CapacityService --------------------------------------------------------


class RemoteCapacityService(_RemoteBase):
    """HTTP-backed counterpart to `application.capacity_service.CapacityService`."""

    def request_capacity(
        self,
        pool_id: str,
        requester_member_id: str,
        target_member_id: str,
        window_type: WindowType,
        amount: int,
        type: CapacityType,
        message: str | None = None,
    ) -> CapacityRequest:
        body = self._request(
            "POST",
            "/capacity/requests",
            json={
                "pool_id": pool_id,
                "requester_member_id": requester_member_id,
                "target_member_id": target_member_id,
                "window_type": window_type.value,
                "amount": amount,
                "type": type.value,
                "message": message,
            },
        ).json()
        return _capacity_request_from_json(body)

    def approve_request(self, request_id: str, approving_member_id: str) -> CapacityGrant:
        body = self._request(
            "POST",
            f"/capacity/requests/{request_id}/approve",
            json={"approving_member_id": approving_member_id},
        ).json()
        return _capacity_grant_from_json(body)

    def reject_request(self, request_id: str, rejecting_member_id: str) -> CapacityRequest:
        body = self._request(
            "POST",
            f"/capacity/requests/{request_id}/reject",
            json={"rejecting_member_id": rejecting_member_id},
        ).json()
        return _capacity_request_from_json(body)

    def revoke_grant(self, grant_id: str, revoking_member_id: str) -> CapacityGrant:
        body = self._request(
            "POST",
            f"/capacity/grants/{grant_id}/revoke",
            json={"revoking_member_id": revoking_member_id},
        ).json()
        return _capacity_grant_from_json(body)

    def get_effective_capacity(self, member_id: str, window_type: WindowType) -> EffectiveCapacity:
        body = self._request(
            "GET", f"/members/{member_id}/capacity", params={"window": window_type.value}
        ).json()
        return EffectiveCapacity(
            member_id=body["member_id"],
            window_type=WindowType(body["window_type"]),
            base_allocation_units=body["base_allocation_units"],
            solid_sent=body["solid_sent"],
            solid_received=body["solid_received"],
            guaranteed_units=body["guaranteed_units"],
            shared_offered=body["shared_offered"],
            shared_borrowed_potential=body["shared_borrowed_potential"],
            potential_units=body["potential_units"],
        )


# --- AgentService --------------------------------------------------------


class RemoteAgentService(_RemoteBase):
    """HTTP-backed counterpart to `application.agent_service.AgentService`.

    `register_device()` never sends a bearer token (matches the server's
    bootstrap exception - see `server/auth.py`) even if this instance was
    constructed with one; it's the one call in this whole module that
    still works before this machine has a token of its own.
    """

    def register_device(self, user_id: str, device_name: str) -> Device:
        response = self._client.post("/devices", json={"user_id": user_id, "device_name": device_name})
        _raise_for_status(response)
        body = response.json()
        return _device_from_json(body["device"], token=body["token"])

    def list_devices(self, user_id: str) -> list[Device]:
        body = self._request("GET", f"/users/{user_id}/devices").json()
        return [_device_from_json(d) for d in body]


# --- JSON -> dataclass converters --------------------------------------------


def _pool_from_json(data: dict) -> Pool:
    return Pool(id=data["id"], name=data["name"], member_count=data["member_count"])


def _member_from_json(data: dict) -> Member:
    return Member(id=data["id"], pool_id=data["pool_id"], user_id=data["user_id"], display_name=data["display_name"])


def _device_from_json(data: dict, token: str | None = None) -> Device:
    return Device(
        id=data["id"],
        user_id=data["user_id"],
        device_name=data["device_name"],
        created_at=_parse_dt(data["created_at"]),
        token_hash="",  # never sent over the wire - not knowable client-side
        token=token,
    )


def _window_status_from_json(data: dict) -> WindowStatus:
    return WindowStatus(
        window_type=WindowType(data["window_type"]),
        allocation_units=data["allocation_units"],
        used_units=data["used_units"],
        remaining_units=data["remaining_units"],
        window_start=_parse_dt(data["window_start"]),
        reset_at=_parse_dt(data["reset_at"]),
    )


def _member_status_from_json(data: dict) -> MemberStatus:
    return MemberStatus(
        member_id=data["member_id"],
        pool_id=data["pool_id"],
        display_name=data["display_name"],
        windows={WindowType(wt): _window_status_from_json(ws) for wt, ws in data["windows"].items()},
    )


def _consume_result_from_json(data: dict) -> ConsumeResult:
    return ConsumeResult(
        member_id=data["member_id"],
        window_type=WindowType(data["window_type"]),
        amount=data["amount"],
        idempotency_key=data["idempotency_key"],
        accepted=data["accepted"],
        replayed=data["replayed"],
        allocation_units=data["allocation_units"],
        remaining_units=data["remaining_units"],
        shared_units_used=data["shared_units_used"],
        shared_draws=tuple(SharedDraw(d["grant_id"], d["amount"]) for d in data["shared_draws"]),
        reason=data["reason"],
    )


def _capacity_request_from_json(data: dict) -> CapacityRequest:
    return CapacityRequest(
        id=data["id"],
        pool_id=data["pool_id"],
        requester_member_id=data["requester_member_id"],
        target_member_id=data["target_member_id"],
        window_type=WindowType(data["window_type"]),
        amount=data["amount"],
        type=CapacityType(data["type"]),
        status=RequestStatus(data["status"]),
        created_at=_parse_dt(data["created_at"]),
        approved_at=_parse_dt(data["approved_at"]),
        expires_at=_parse_dt(data["expires_at"]),
        message=data["message"],
    )


def _capacity_grant_from_json(data: dict) -> CapacityGrant:
    return CapacityGrant(
        id=data["id"],
        pool_id=data["pool_id"],
        source_member_id=data["source_member_id"],
        recipient_member_id=data["recipient_member_id"],
        window_type=WindowType(data["window_type"]),
        amount=data["amount"],
        type=CapacityType(data["type"]),
        status=GrantStatus(data["status"]),
        created_at=_parse_dt(data["created_at"]),
        activated_at=_parse_dt(data["activated_at"]),
        expires_at=_parse_dt(data["expires_at"]),
        revoked_at=_parse_dt(data["revoked_at"]),
    )


def build_remote_services(
    server_url: str, device_token: str | None, client: httpx.Client | None = None
) -> tuple[RemoteQuotaService, RemoteCapacityService, RemoteAgentService]:
    """Convenience used by `cli/main.py` - all three remote services share
    one underlying `httpx.Client` (connection pooling, one place to close)
    when `client` isn't supplied explicitly (tests inject their own, e.g.
    an `httpx.Client(transport=ASGITransport(...))` bound to an in-process
    FastAPI app)."""
    shared_client = client or httpx.Client(base_url=server_url, timeout=DEFAULT_TIMEOUT_SECONDS)
    return (
        RemoteQuotaService(server_url, device_token, client=shared_client),
        RemoteCapacityService(server_url, device_token, client=shared_client),
        RemoteAgentService(server_url, device_token, client=shared_client),
    )
