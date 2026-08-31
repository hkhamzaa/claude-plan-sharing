"""Application service for device/identity bookkeeping (Milestone 3).

AgentService is deliberately small: it only registers and looks up
Devices tied to an existing Member's user_id. It does not change quota
math in any way - consume()/check_quota() are still keyed purely by
member_id, exactly as in Milestones 1-2. See docs/architecture.md for how
this fits into the local-identity layer (`claude_share.agent`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from claude_share.application.ids import new_id
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
        """
        if not device_name:
            raise ValueError("device_name must not be empty")

        now = datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            member = uow.members.find_by_user_id(user_id)
            if member is None:
                uow.rollback()
                raise UserNotFoundError(user_id)

            device = Device(id=new_id(), user_id=user_id, device_name=device_name, created_at=now)
            uow.devices.add(device)
            uow.commit()

        return device

    def list_devices(self, user_id: str) -> list[Device]:
        with self._uow_factory() as uow:
            devices = uow.devices.list_by_user(user_id)
            uow.commit()
        return devices
