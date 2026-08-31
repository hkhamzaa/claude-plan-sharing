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

from claude_share.agent.errors import NotLoggedInError
from claude_share.agent.identity import LocalIdentity, load_local_identity, save_local_identity
from claude_share.application.agent_service import AgentService
from claude_share.application.capacity_service import CapacityService
from claude_share.application.dto import EffectiveCapacity, MemberStatus
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import MemberNotFoundError, MemberNotInPoolError, MemberNotOwnedByUserError
from claude_share.domain.models import WindowType
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
