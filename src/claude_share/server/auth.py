"""Milestone 5 device-token authentication for the central server.

## The mechanism, and why it's intentionally this simple

Every request except two bootstrap endpoints (`POST /pools`,
`POST /devices` - see below) must carry
`Authorization: Bearer <token>`,
where `<token>` is the plaintext value `AgentService.register_device()`
returned exactly once, at registration. `get_current_device()` resolves
that header back to a `Device` via `AgentService.verify_device_token()`,
which hashes the presented token and looks it up by `Device.token_hash` -
the plaintext token is never stored, so a leaked database dump alone
cannot be used to authenticate as anyone. From there, `require_member()`
resolves *which member_id* a request may act as: it re-reads the target
`member_id` fresh from storage and checks `member.user_id ==
device.user_id`, i.e. "does the device's own user_id actually own this
member?" - exactly the same check `agent.commands.join_pool()` already
does locally (Milestone 3), just re-run server-side on every request
instead of once at `join` time, since an HTTP request has no persistent
session to trust between calls.

This is deliberately the simplest mechanism that actually authenticates,
not a placeholder for something more elaborate: no OAuth, no JWT, no
expiry/refresh flow, no scopes. Per the reminder that shaped this
milestone, the trust model here is a small, cooperative, trusted group -
the threat this defends against is "a stray or misconfigured device
accidentally (or a stranger who doesn't know anyone's token at all) acting
as someone else over the network," not a sophisticated adversary within
the group. An opaque 256-bit bearer token, hashed at rest, checked on
every request, is already more than sufficient for that; anything more
elaborate (rotation policies, scoped tokens, short-lived JWTs) would be
security theater relative to the actual risk this milestone is defending
against, and was explicitly called out as out of scope. See
docs/architecture.md for the fuller writeup, including why this is *not*
a substitute for running the server behind TLS (a bearer token sent over
plain HTTP is exactly as sniffable as a password would be).

## Why `POST /pools` and `POST /devices` don't require a token

These are the only two bootstrap operations where no prior identity can
exist to check against: `create_pool()` is how the very first
members/user_ids in a fresh deployment come to exist at all (no pool
exists yet, so no device/token can be scoped to one), and
`register_device()` is how a user_id-holder gets their *first* token -
requiring a token to obtain a token is circular. This exactly mirrors the
local CLI's existing trust model (Milestone 1's `pool create` never
required an identity either, and Milestone 3's `login` treats "knowing a
valid user_id" - itself a random UUID nobody can guess, freshly minted by
`create_pool()` and shared out-of-band by whoever ran it - as the only
"credential" needed to register a device). What changes in Milestone 5 is
that registration now also mints a *real*, hashed, per-device credential
for every request after that point - see `application/agent_service.py`.

Listing a pool's members is not in this category: the caller who just
created the pool already knows its members from the `POST /pools`
response body, and every other caller can present a valid bearer token.
`GET /pools/{pool_id}/members` therefore requires authentication like
every other endpoint.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Header, HTTPException, Request

from claude_share.application.agent_service import AgentService
from claude_share.domain.models import Device, Member
from claude_share.domain.repository import UnitOfWork


def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service


def get_uow_factory(request: Request) -> Callable[[], UnitOfWork]:
    return request.app.state.uow_factory


async def get_current_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Device:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    agent_service: AgentService = request.app.state.agent_service
    device = agent_service.verify_device_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="Invalid or unknown device token.")
    return device


def require_member(uow_factory: Callable[[], UnitOfWork], device: Device, member_id: str) -> Member:
    """Resolve `member_id`, and confirm the authenticated device's user_id
    actually owns it - re-checked fresh on every call, not cached from
    registration time. Raises 404 for an unknown member_id, 403 for a
    member_id that exists but belongs to someone else's user_id."""
    with uow_factory() as uow:
        member = uow.members.get(member_id)
        uow.commit()

    if member is None:
        raise HTTPException(status_code=404, detail=f"Member {member_id!r} not found.")
    if member.user_id != device.user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Device {device.id!r} is not authorized to act as member {member_id!r}.",
        )
    return member
