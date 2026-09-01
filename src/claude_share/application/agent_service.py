"""Application service for device/identity bookkeeping (Milestone 3),
extended in Milestone 5 with device API token issuance/verification.

AgentService is deliberately small: it only registers and looks up
Devices tied to an existing Member's user_id. It does not change quota
math in any way - consume()/check_quota() are still keyed purely by
member_id, exactly as in Milestones 1-2. See docs/architecture.md for how
this fits into the local-identity layer (`claude_share.agent`) and, for
Milestone 5, into the central server's auth (`claude_share.server`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from claude_share.application.ids import new_id
from claude_share.application.tokens import generate_device_token, hash_device_token
from claude_share.domain.errors import UserNotFoundError
from claude_share.domain.models import Device
from claude_share.domain.repository import UnitOfWork


class AgentService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def register_device(self, user_id: str, device_name: str) -> Device:
        """Create a Device for an existing user_id.

        `user_id` must already belong to some Member somewhere in the
        system (this milestone has no account-creation flow - a user_id
        only becomes "real" by being minted for a Member via
        `QuotaService.create_pool()`).

        As of Milestone 5, every registration also mints a fresh opaque
        bearer token (`claude_share.application.tokens`): only its hash is
        persisted (`Device.token_hash`); the plaintext value is set on the
        returned `Device.token` and nowhere else - this is the one and only
        time it is ever available. Callers that don't need the token
        (Milestone 3's local-only `login()`) simply never read `.token`;
        nothing about their behavior changes.
        """
        if not device_name:
            raise ValueError("device_name must not be empty")

        now = datetime.now(timezone.utc)
        token = generate_device_token()
        token_hash = hash_device_token(token)

        with self._uow_factory() as uow:
            member = uow.members.find_by_user_id(user_id)
            if member is None:
                uow.rollback()
                raise UserNotFoundError(user_id)

            device = Device(
                id=new_id(),
                user_id=user_id,
                device_name=device_name,
                created_at=now,
                token_hash=token_hash,
                token=token,
            )
            uow.devices.add(device)
            uow.commit()

        return device

    def list_devices(self, user_id: str) -> list[Device]:
        with self._uow_factory() as uow:
            devices = uow.devices.list_by_user(user_id)
            uow.commit()
        return devices

    def verify_device_token(self, token: str) -> Device | None:
        """Resolve a plaintext bearer token to the Device that owns it.

        Used exclusively by the Milestone 5 server's auth dependency
        (`claude_share.server.auth`) to turn an `Authorization: Bearer ...`
        header into a `Device` (and, via `Device.user_id`, the member(s) it
        may act as) - never by local-only CLI code paths, which have no
        server to authenticate against. Returns None for an unknown/revoked
        token; never raises for that case, since "invalid token" is an
        ordinary, expected auth outcome, not a domain error.
        """
        with self._uow_factory() as uow:
            device = uow.devices.find_by_token_hash(hash_device_token(token))
            uow.commit()
        return device
