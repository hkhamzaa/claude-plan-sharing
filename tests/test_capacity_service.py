from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_share.application.capacity_service import CapacityService
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import (
    InsufficientSourceCapacityError,
    NotAuthorizedError,
)
from claude_share.domain.models import CapacityType, GrantStatus, RequestStatus, WindowType
from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork


# --- SOLID: transfer semantics -------------------------------------------


def test_solid_transfer_updates_guaranteed_and_blocks_source_from_overspending(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 1000, CapacityType.SOLID
    )
    grant = capacity_service.approve_request(request.id, alice.id)
    assert grant.status is GrantStatus.ACTIVE
    assert grant.type is CapacityType.SOLID
    assert grant.source_member_id == alice.id
    assert grant.recipient_member_id == bob.id

    alice_capacity = capacity_service.get_effective_capacity(alice.id, WindowType.FIVE_HOUR)
    bob_capacity = capacity_service.get_effective_capacity(bob.id, WindowType.FIVE_HOUR)
    assert alice_capacity.guaranteed_units == base - 1000
    assert bob_capacity.guaranteed_units == base + 1000

    # Alice cannot spend the transferred amount.
    overreach = service.consume(alice.id, WindowType.FIVE_HOUR, base - 1000 + 1, "alice-overreach")
    assert overreach.accepted is False

    within_new_limit = service.consume(alice.id, WindowType.FIVE_HOUR, base - 1000, "alice-max")
    assert within_new_limit.accepted is True


def test_solid_recipient_can_consume_beyond_own_base_allocation(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    """Regression test: a SOLID recipient's guaranteed capacity can exceed
    their own window's base allocation_units. consume() must accept an
    amount in that gap purely from guaranteed capacity, with no SHARED
    grant involved - this used to raise ValueError("usage_units must not
    exceed allocation_units") because QuotaWindow's invariant incorrectly
    assumed usage_units could never exceed the window's own base
    allocation_units, which stopped being true the moment SOLID grants
    could push a recipient's real ceiling above it."""
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(bob.id).windows[WindowType.FIVE_HOUR].allocation_units  # 5000

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 1000, CapacityType.SOLID
    )
    capacity_service.approve_request(request.id, alice.id)  # bob's guaranteed becomes base + 1000

    first = service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-uses-full-base")
    assert first.accepted is True
    assert first.shared_units_used == 0

    # This amount only fits within bob's guaranteed capacity (base + 1000),
    # not his own base allocation_units - must succeed without touching any
    # SHARED grant (there is none) and without raising.
    second = service.consume(bob.id, WindowType.FIVE_HOUR, 500, "bob-uses-solid-received")
    assert second.accepted is True
    assert second.shared_units_used == 0
    assert second.remaining_units == base + 1000 - base - 500  # == 500

    status = service.get_status(bob.id).windows[WindowType.FIVE_HOUR]
    assert status.used_units == base + 500


def test_solid_approve_rejected_when_source_already_over_committed(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob", "Carol"])
    alice, bob, carol = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units

    first_request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, base, CapacityType.SOLID
    )
    capacity_service.approve_request(first_request.id, alice.id)  # sends everything away

    second_request = capacity_service.request_capacity(
        pool.id, carol.id, alice.id, WindowType.FIVE_HOUR, 1, CapacityType.SOLID
    )
    with pytest.raises(InsufficientSourceCapacityError):
        capacity_service.approve_request(second_request.id, alice.id)


# --- SHARED: conditional, priority-respecting access ----------------------


def test_shared_recipient_can_consume_from_idle_source(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(bob.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 1000, CapacityType.SHARED
    )
    capacity_service.approve_request(request.id, alice.id)

    service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-own")  # exhaust Bob's own pool
    result = service.consume(bob.id, WindowType.FIVE_HOUR, 200, "bob-shared")

    assert result.accepted is True
    assert result.shared_units_used == 200
    assert len(result.shared_draws) == 1

    # The draw comes out of Alice's own ledger, not Bob's.
    alice_status = service.get_status(alice.id).windows[WindowType.FIVE_HOUR]
    assert alice_status.used_units == 200


def test_shared_owner_priority_reduces_what_recipient_can_draw(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units  # 5000

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 2000, CapacityType.SHARED
    )
    capacity_service.approve_request(request.id, alice.id)

    # Alice uses most of her own capacity first - she has priority.
    alice_result = service.consume(alice.id, WindowType.FIVE_HOUR, 4000, "alice-uses-most")
    assert alice_result.accepted is True

    service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-own")  # exhaust Bob's own pool

    # Only 1000 unused remains with Alice, even though the grant ceiling is 2000.
    too_much = service.consume(bob.id, WindowType.FIVE_HOUR, 1500, "bob-too-much")
    assert too_much.accepted is False  # all-or-nothing: no partial draw

    exactly_available = service.consume(bob.id, WindowType.FIVE_HOUR, 1000, "bob-exact")
    assert exactly_available.accepted is True
    assert exactly_available.shared_units_used == 1000

    alice_status = service.get_status(alice.id).windows[WindowType.FIVE_HOUR]
    assert alice_status.used_units == 5000  # fully used now (4000 own + 1000 drawn by Bob)


def test_shared_multiple_requests_reject_when_exceeding_source_base(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob", "Carol"])
    alice, bob, carol = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.WEEKLY].allocation_units

    first_request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.WEEKLY, base - 100, CapacityType.SHARED
    )
    capacity_service.approve_request(first_request.id, alice.id)

    over_limit_request = capacity_service.request_capacity(
        pool.id, carol.id, alice.id, WindowType.WEEKLY, 200, CapacityType.SHARED
    )
    with pytest.raises(InsufficientSourceCapacityError):
        capacity_service.approve_request(over_limit_request.id, alice.id)

    exact_headroom_request = capacity_service.request_capacity(
        pool.id, carol.id, alice.id, WindowType.WEEKLY, 100, CapacityType.SHARED
    )
    grant = capacity_service.approve_request(exact_headroom_request.id, alice.id)
    assert grant.status is GrantStatus.ACTIVE


def test_shared_grant_lifetime_cap_enforced_across_multiple_consumes(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(bob.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 300, CapacityType.SHARED
    )
    capacity_service.approve_request(request.id, alice.id)
    service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-own")

    first_draw = service.consume(bob.id, WindowType.FIVE_HOUR, 200, "draw-1")
    assert first_draw.accepted is True
    assert first_draw.shared_units_used == 200

    # Grant only has 100 left (300 - 200), even though Alice has plenty of her
    # own unused capacity - the grant's own lifetime ceiling binds here.
    second_draw = service.consume(bob.id, WindowType.FIVE_HOUR, 150, "draw-2")
    assert second_draw.accepted is False

    alice_status = service.get_status(alice.id).windows[WindowType.FIVE_HOUR]
    assert alice_status.used_units == 200  # rejected draw-2 left Alice's usage unchanged


def test_consume_idempotent_replay_reports_same_shared_draws(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(bob.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 500, CapacityType.SHARED
    )
    capacity_service.approve_request(request.id, alice.id)
    service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-own")

    first = service.consume(bob.id, WindowType.FIVE_HOUR, 300, "bob-shared-key")
    second = service.consume(bob.id, WindowType.FIVE_HOUR, 300, "bob-shared-key")

    assert first.shared_units_used == 300
    assert second.replayed is True
    assert second.shared_units_used == 300
    assert first.shared_draws == second.shared_draws

    alice_status = service.get_status(alice.id).windows[WindowType.FIVE_HOUR]
    assert alice_status.used_units == 300  # not double-drawn on replay


# --- revoke_grant ----------------------------------------------------------


def test_revoke_solid_restores_source_capacity_immediately(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 1000, CapacityType.SOLID
    )
    grant = capacity_service.approve_request(request.id, alice.id)
    assert capacity_service.get_effective_capacity(alice.id, WindowType.FIVE_HOUR).guaranteed_units == base - 1000

    revoked = capacity_service.revoke_grant(grant.id, alice.id)
    assert revoked.status is GrantStatus.REVOKED
    assert revoked.revoked_at is not None

    assert capacity_service.get_effective_capacity(alice.id, WindowType.FIVE_HOUR).guaranteed_units == base
    assert capacity_service.get_effective_capacity(bob.id, WindowType.FIVE_HOUR).guaranteed_units == base


def test_revoke_shared_removes_recipient_access_immediately(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(bob.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 1000, CapacityType.SHARED
    )
    grant = capacity_service.approve_request(request.id, alice.id)

    capacity_service.revoke_grant(grant.id, alice.id)

    service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-own")
    result = service.consume(bob.id, WindowType.FIVE_HOUR, 1, "bob-after-revoke")
    assert result.accepted is False


# --- grant expiration -------------------------------------------------------


def test_expired_grant_is_not_usable_by_consume(
    service: QuotaService, capacity_service: CapacityService, db_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(bob.id).windows[WindowType.FIVE_HOUR].allocation_units

    # Force Alice's five_hour window to already be "in the past" so the grant
    # we approve next inherits an already-expired expires_at (the default
    # expiration policy ties a grant to the source's window reset_at).
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with SqliteUnitOfWork(db_path) as uow:
        window = uow.windows.get(alice.id, WindowType.FIVE_HOUR)
        uow.windows.update(replace(window, reset_at=past))
        uow.commit()

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 1000, CapacityType.SHARED
    )
    grant = capacity_service.approve_request(request.id, alice.id)
    assert grant.expires_at == past

    service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-own")
    result = service.consume(bob.id, WindowType.FIVE_HOUR, 100, "bob-tries-expired-grant")
    assert result.accepted is False

    # Lazily expired as a side effect of being touched.
    with SqliteUnitOfWork(db_path) as uow:
        stored_grant = uow.grants.get(grant.id)
        uow.commit()
    assert stored_grant.status is GrantStatus.EXPIRED


# --- authorization -----------------------------------------------------------


def test_only_target_member_can_approve(service: QuotaService, capacity_service: CapacityService) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob", "Carol"])
    alice, bob, carol = service.list_members(pool.id)
    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 100, CapacityType.SOLID
    )

    with pytest.raises(NotAuthorizedError):
        capacity_service.approve_request(request.id, carol.id)
    with pytest.raises(NotAuthorizedError):
        capacity_service.approve_request(request.id, bob.id)  # requester cannot self-approve


def test_only_target_member_can_reject(service: QuotaService, capacity_service: CapacityService) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 100, CapacityType.SOLID
    )
    with pytest.raises(NotAuthorizedError):
        capacity_service.reject_request(request.id, bob.id)


def test_only_source_member_can_revoke(service: QuotaService, capacity_service: CapacityService) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, 100, CapacityType.SOLID
    )
    grant = capacity_service.approve_request(request.id, alice.id)
    with pytest.raises(NotAuthorizedError):
        capacity_service.revoke_grant(grant.id, bob.id)


# --- atomicity ---------------------------------------------------------------


def test_approve_rejected_leaves_request_pending_and_creates_no_grant(
    service: QuotaService, capacity_service: CapacityService, db_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, base + 1, CapacityType.SOLID
    )
    with pytest.raises(InsufficientSourceCapacityError):
        capacity_service.approve_request(request.id, alice.id)

    with SqliteUnitOfWork(db_path) as uow:
        stored_request = uow.requests.get(request.id)
        uow.commit()
    assert stored_request.status is RequestStatus.PENDING
    assert stored_request.approved_at is None

    conn = sqlite3.connect(str(db_path))
    try:
        grant_count = conn.execute("SELECT COUNT(*) FROM capacity_grants").fetchone()[0]
    finally:
        conn.close()
    assert grant_count == 0


# --- concurrency ---------------------------------------------------------------


def test_concurrent_approve_requests_only_one_shared_grant_survives(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob", "Carol"])
    alice, bob, carol = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units

    # Each request alone fits within `base`; approving both would exceed it.
    amount = (base // 2) + 100
    bob_request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, amount, CapacityType.SHARED
    )
    carol_request = capacity_service.request_capacity(
        pool.id, carol.id, alice.id, WindowType.FIVE_HOUR, amount, CapacityType.SHARED
    )

    def approve(request_id: str):
        try:
            return ("ok", capacity_service.approve_request(request_id, alice.id))
        except InsufficientSourceCapacityError as exc:
            return ("rejected", exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(approve, bob_request.id), executor.submit(approve, carol_request.id)]
        outcomes = [f.result()[0] for f in as_completed(futures)]

    assert outcomes.count("ok") == 1
    assert outcomes.count("rejected") == 1

    effective = capacity_service.get_effective_capacity(alice.id, WindowType.FIVE_HOUR)
    assert effective.shared_offered == amount  # only the surviving grant counted
    assert effective.shared_offered <= base


def test_shared_concurrent_consumption_never_oversells_source(
    service: QuotaService, capacity_service: CapacityService
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice, bob = service.list_members(pool.id)
    base = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].allocation_units  # 5000

    request = capacity_service.request_capacity(
        pool.id, bob.id, alice.id, WindowType.FIVE_HOUR, base, CapacityType.SHARED
    )
    capacity_service.approve_request(request.id, alice.id)
    service.consume(bob.id, WindowType.FIVE_HOUR, base, "bob-exhausts-own")

    amount_each = 100
    rounds = base // amount_each  # exactly enough calls (split between Alice and Bob) to exhaust Alice

    def alice_attempt(i: int):
        return service.consume(alice.id, WindowType.FIVE_HOUR, amount_each, f"alice-{i}")

    def bob_attempt(i: int):
        return service.consume(bob.id, WindowType.FIVE_HOUR, amount_each, f"bob-shared-{i}")

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for i in range(rounds):
            futures.append(executor.submit(alice_attempt, i))
            futures.append(executor.submit(bob_attempt, i))
        for future in as_completed(futures):
            results.append(future.result())

    accepted_amount = sum(r.amount for r in results if r.accepted)
    assert accepted_amount <= base  # Alice's total capacity (own + drawn by Bob) never oversold

    alice_status = service.get_status(alice.id).windows[WindowType.FIVE_HOUR]
    assert alice_status.used_units == accepted_amount
    assert alice_status.used_units <= base
