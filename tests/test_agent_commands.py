from __future__ import annotations

from pathlib import Path

import pytest

from claude_share.agent.commands import agent_status, join_pool, login
from claude_share.agent.errors import NotLoggedInError
from claude_share.agent.identity import LocalIdentity, load_local_identity, save_local_identity
from claude_share.application.capacity_service import CapacityService
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import MemberNotInPoolError, MemberNotOwnedByUserError, UserNotFoundError
from claude_share.domain.models import WindowType


# --- local identity config file round-trip --------------------------------


def test_local_identity_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    identity = LocalIdentity(
        pool_id="pool-1", member_id="member-1", user_id="user-1", device_id="device-1", device_name="Laptop"
    )
    save_local_identity(path, identity)
    loaded = load_local_identity(path)
    assert loaded == identity


def test_load_local_identity_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_local_identity(tmp_path / "does-not-exist.json") is None


def test_local_identity_round_trip_with_unjoined_pool_member(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    identity = LocalIdentity(pool_id=None, member_id=None, user_id="user-1", device_id="device-1", device_name="Laptop")
    save_local_identity(path, identity)
    assert load_local_identity(path) == identity


# --- login ------------------------------------------------------------------


def test_login_writes_valid_local_identity(service: QuotaService, uow_factory, config_path: Path) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]

    identity = login(config_path, uow_factory, alice.user_id, "Alice's Laptop")

    assert identity.user_id == alice.user_id
    assert identity.device_name == "Alice's Laptop"
    assert identity.pool_id is None
    assert identity.member_id is None

    loaded = load_local_identity(config_path)
    assert loaded == identity


def test_login_raises_for_nonexistent_user_id(uow_factory, config_path: Path) -> None:
    with pytest.raises(UserNotFoundError):
        login(config_path, uow_factory, "no-such-user", "Some Device")
    assert load_local_identity(config_path) is None  # no partial config written


# --- join_pool ----------------------------------------------------------------


def test_join_pool_fails_before_login(service: QuotaService, uow_factory, config_path: Path) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    with pytest.raises(NotLoggedInError):
        join_pool(config_path, uow_factory, pool.id, alice.id)


def test_join_pool_fails_if_member_not_in_pool(service: QuotaService, uow_factory, config_path: Path) -> None:
    pool_a = service.create_pool("Pool A", ["Alice"])
    pool_b = service.create_pool("Pool B", ["Bob"])
    alice = service.list_members(pool_a.id)[0]
    bob = service.list_members(pool_b.id)[0]

    login(config_path, uow_factory, alice.user_id, "Alice's Laptop")

    with pytest.raises(MemberNotInPoolError):
        join_pool(config_path, uow_factory, pool_a.id, bob.id)  # bob belongs to pool_b, not pool_a

    # Failed join must not mutate the existing local identity.
    identity = load_local_identity(config_path)
    assert identity.pool_id is None
    assert identity.member_id is None


def test_join_pool_fails_if_member_belongs_to_different_user(
    service: QuotaService, uow_factory, config_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)

    login(config_path, uow_factory, alice.user_id, "Alice's Laptop")

    # bob.id legitimately belongs to `pool`, but not to alice's user_id.
    with pytest.raises(MemberNotOwnedByUserError):
        join_pool(config_path, uow_factory, pool.id, bob.id)

    # Failed join must not mutate the existing local identity.
    identity = load_local_identity(config_path)
    assert identity.pool_id is None
    assert identity.member_id is None


def test_join_pool_succeeds_and_updates_config_file(service: QuotaService, uow_factory, config_path: Path) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]

    login(config_path, uow_factory, alice.user_id, "Alice's Laptop")
    identity = join_pool(config_path, uow_factory, pool.id, alice.id)

    assert identity.pool_id == pool.id
    assert identity.member_id == alice.id
    # Identity fields carried over unchanged.
    assert identity.user_id == alice.user_id

    reloaded = load_local_identity(config_path)
    assert reloaded == identity


# --- agent_status --------------------------------------------------------------


def test_agent_status_not_logged_in(
    config_path: Path, service: QuotaService, capacity_service: CapacityService
) -> None:
    view = agent_status(config_path, service, capacity_service)
    assert view.logged_in is False
    assert view.joined_pool is False
    assert view.identity is None
    assert view.member_status is None


def test_agent_status_logged_in_but_not_joined(
    service: QuotaService, capacity_service: CapacityService, uow_factory, config_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    login(config_path, uow_factory, alice.user_id, "Alice's Laptop")

    view = agent_status(config_path, service, capacity_service)
    assert view.logged_in is True
    assert view.joined_pool is False
    assert view.identity is not None
    assert view.member_status is None
    assert view.effective_capacity is None


def test_agent_status_combined_view_when_logged_in_and_joined(
    service: QuotaService, capacity_service: CapacityService, uow_factory, config_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    login(config_path, uow_factory, alice.user_id, "Alice's Laptop")
    join_pool(config_path, uow_factory, pool.id, alice.id)

    service.consume(alice.id, WindowType.FIVE_HOUR, 100, "consume-key-1")

    view = agent_status(config_path, service, capacity_service)

    assert view.logged_in is True
    assert view.joined_pool is True
    assert view.member_status.member_id == alice.id
    assert view.member_status.windows[WindowType.FIVE_HOUR].used_units == 100
    assert view.effective_capacity[WindowType.FIVE_HOUR].guaranteed_units == 5000
    assert view.effective_capacity[WindowType.WEEKLY].guaranteed_units == 5000
