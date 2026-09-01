"""Logic behind the local-agent CLI commands (login/join/whoami), kept
separate from argparse wiring so it's independently testable.

Each function here composes existing application-layer services
(`AgentService`, `QuotaService`, `CapacityService`) with the local identity
file (`agent/identity.py`) - none of them introduce new quota-math or
persistence concerns of their own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import httpx

from claude_share.agent.errors import NotLoggedInError
from claude_share.agent.identity import LocalIdentity, load_local_identity, save_local_identity
from claude_share.agent.remote_client import RemoteAgentService
from claude_share.application.agent_service import AgentService
from claude_share.application.capacity_service import CapacityService
from claude_share.application.dto import EffectiveCapacity, MemberStatus
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import MemberNotFoundError, MemberNotInPoolError, MemberNotOwnedByUserError
from claude_share.domain.models import Member, WindowType
from claude_share.domain.repository import UnitOfWork


def login(
    config_path: str | Path,
    uow_factory: Callable[[], UnitOfWork],
    user_id: str,
    device_name: str,
) -> LocalIdentity:
    """Point this machine at an existing user_id.

    Raises `UserNotFoundError` (via `AgentService.register_device`) if
    `user_id` doesn't belong to any existing Member - this milestone has no
    account-creation flow, so "login" only recognizes identities that
    already exist. See docs/architecture.md for why this isn't real
    authentication.
    """
    agent_service = AgentService(uow_factory)
    device = agent_service.register_device(user_id, device_name)

    identity = LocalIdentity(
        pool_id=None,
        member_id=None,
        user_id=user_id,
        device_id=device.id,
        device_name=device.device_name,
    )
    save_local_identity(config_path, identity)
    return identity


def join_pool(
    config_path: str | Path,
    uow_factory: Callable[[], UnitOfWork],
    pool_id: str,
    member_id: str,
) -> LocalIdentity:
    """Point this machine's local identity at a specific pool_id/member_id.

    Requires `login()` to have already run (there must be a local identity
    to update). Verifies both that `member_id` actually belongs to
    `pool_id` and that it belongs to the already-logged-in `user_id`,
    rejecting either mismatch as an input error - e.g. a typo'd
    `member_id` that happens to point at someone else's identity.
    """
    current = load_local_identity(config_path)
    if current is None:
        raise NotLoggedInError()

    with uow_factory() as uow:
        member = uow.members.get(member_id)
        if member is None:
            uow.rollback()
            raise MemberNotFoundError(member_id)
        if member.pool_id != pool_id:
            uow.rollback()
            raise MemberNotInPoolError(member_id, pool_id)
        if member.user_id != current.user_id:
            uow.rollback()
            raise MemberNotOwnedByUserError(member_id, current.user_id)
        uow.commit()

    updated = replace(current, pool_id=pool_id, member_id=member_id)
    save_local_identity(config_path, updated)
    return updated


class _ListsMembers(Protocol):
    """The one method `join_pool_remote` needs - satisfied by both
    `QuotaService` and `agent.remote_client.RemoteQuotaService` without
    either needing to import the other."""

    def list_members(self, pool_id: str) -> list[Member]: ...


def login_remote(
    config_path: str | Path,
    server_url: str,
    user_id: str,
    device_name: str,
    client: httpx.Client | None = None,
) -> LocalIdentity:
    """Remote (Milestone 5) counterpart to `login()`: registers this
    machine as a Device against a central server over HTTP instead of the
    local SQLite database, and stores the bearer token that comes back in
    the local identity file so every later command run in this config can
    authenticate. Everything else about the local identity file's shape
    and the "explicit CLI arg beats local identity" resolution rules
    (docs/architecture.md, Milestone 3) is unchanged - the only new fields
    are `server_url`/`device_token`, which is also how `cli/main.py`
    decides this identity means "talk HTTP," not "open SQLite."

    Raises `agent.errors.RemoteRequestError` for an unknown user_id - the
    remote equivalent of `login()`'s `UserNotFoundError` (see that
    exception's docstring for why status codes aren't reconstructed back
    into specific domain-error subtypes; both are caught identically by
    `cli/main.py:main()`).
    """
    agent_service = RemoteAgentService(server_url, device_token=None, client=client)
    device = agent_service.register_device(user_id, device_name)

    identity = LocalIdentity(
        pool_id=None,
        member_id=None,
        user_id=user_id,
        device_id=device.id,
        device_name=device.device_name,
        server_url=server_url,
        device_token=device.token,
    )
    save_local_identity(config_path, identity)
    return identity


def join_pool_remote(config_path: str | Path, quota_service: _ListsMembers, pool_id: str, member_id: str) -> LocalIdentity:
    """Remote counterpart to `join_pool()`: validates membership by reading
    the pool's member list over HTTP (`GET /pools/{pool_id}/members` - any
    authenticated device may read it, see `server/auth.py`) instead of a
    direct `uow.members.get()` read, since there is no dedicated
    "fetch one member by id" endpoint in this milestone's server API. One
    consequence: unlike `join_pool()`, this can't distinguish "member_id
    doesn't exist at all" from "member_id exists but belongs to a
    different pool" - both surface as `MemberNotInPoolError`, a
    deliberate, minor simplification rather than adding a new endpoint
    just to preserve that distinction remotely.
    """
    current = load_local_identity(config_path)
    if current is None:
        raise NotLoggedInError()

    members = quota_service.list_members(pool_id)
    member = next((m for m in members if m.id == member_id), None)
    if member is None:
        raise MemberNotInPoolError(member_id, pool_id)
    if member.user_id != current.user_id:
        raise MemberNotOwnedByUserError(member_id, current.user_id)

    updated = replace(current, pool_id=pool_id, member_id=member_id)
    save_local_identity(config_path, updated)
    return updated


@dataclass(frozen=True, slots=True)
class AgentStatusView:
    """Combined identity + quota-status + effective-capacity view for the CLI."""

    logged_in: bool
    joined_pool: bool
    identity: LocalIdentity | None = None
    member_status: MemberStatus | None = None
    effective_capacity: dict[WindowType, EffectiveCapacity] | None = None


def agent_status(
    config_path: str | Path,
    quota_service: QuotaService,
    capacity_service: CapacityService,
) -> AgentStatusView:
    """Read local identity and, if fully configured, combine it with a live
    quota/capacity snapshot. Never raises for "not logged in" - that's a
    normal, expected state reflected in the returned view instead."""
    identity = load_local_identity(config_path)
    if identity is None:
        return AgentStatusView(logged_in=False, joined_pool=False)

    if identity.pool_id is None or identity.member_id is None:
        return AgentStatusView(logged_in=True, joined_pool=False, identity=identity)

    member_status = quota_service.get_status(identity.member_id)
    effective_capacity = {
        window_type: capacity_service.get_effective_capacity(identity.member_id, window_type)
        for window_type in WindowType
    }

    return AgentStatusView(
        logged_in=True,
        joined_pool=True,
        identity=identity,
        member_status=member_status,
        effective_capacity=effective_capacity,
    )
