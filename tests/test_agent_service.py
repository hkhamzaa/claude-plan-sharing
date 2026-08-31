from __future__ import annotations

import pytest

from claude_share.application.agent_service import AgentService
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import UserNotFoundError


def test_register_device_creates_device_for_valid_user_id(
    service: QuotaService, agent_service: AgentService
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]

    device = agent_service.register_device(alice.user_id, "Alice's Laptop")

    assert device.user_id == alice.user_id
    assert device.device_name == "Alice's Laptop"
    assert device.id


def test_register_device_raises_for_nonexistent_user_id(agent_service: AgentService) -> None:
    with pytest.raises(UserNotFoundError):
        agent_service.register_device("no-such-user-id", "Some Device")


def test_register_device_rejects_empty_device_name(
    service: QuotaService, agent_service: AgentService
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    with pytest.raises(ValueError):
        agent_service.register_device(alice.user_id, "")


def test_list_devices_returns_only_that_users_devices(
    service: QuotaService, agent_service: AgentService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)

    agent_service.register_device(alice.user_id, "Alice's Laptop")
    agent_service.register_device(alice.user_id, "Alice's Phone")
    agent_service.register_device(bob.user_id, "Bob's Laptop")

    alice_devices = agent_service.list_devices(alice.user_id)
    bob_devices = agent_service.list_devices(bob.user_id)

    assert {d.device_name for d in alice_devices} == {"Alice's Laptop", "Alice's Phone"}
    assert {d.device_name for d in bob_devices} == {"Bob's Laptop"}


def test_list_devices_empty_for_user_with_no_devices(
    service: QuotaService, agent_service: AgentService
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    assert agent_service.list_devices(alice.user_id) == []
