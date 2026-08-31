from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import (
    IdempotencyConflictError,
    MemberNotFoundError,
    QuotaWindowNotFoundError,
)
from claude_share.domain.models import TOTAL_ALLOCATION_BPS, WindowType
from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork


# --- create_pool -------------------------------------------------------


def test_create_pool_creates_members_and_equal_allocation(service: QuotaService) -> None:
    pool = service.create_pool("Family Plan", ["Alice", "Bob", "Carol"])
    assert pool.member_count == 3

    members = service.list_members(pool.id)
    assert [m.display_name for m in members] == ["Alice", "Bob", "Carol"]

    total_five_hour = 0
    total_weekly = 0
    for member in members:
        status = service.get_status(member.id)
        five_hour = status.windows[WindowType.FIVE_HOUR]
        weekly = status.windows[WindowType.WEEKLY]
        assert five_hour.used_units == 0
        assert weekly.used_units == 0
        assert five_hour.allocation_units == weekly.allocation_units
        total_five_hour += five_hour.allocation_units
        total_weekly += weekly.allocation_units

    assert total_five_hour == TOTAL_ALLOCATION_BPS
    assert total_weekly == TOTAL_ALLOCATION_BPS


def test_create_pool_rejects_empty_member_list(service: QuotaService) -> None:
    with pytest.raises(ValueError):
        service.create_pool("Empty", [])


# --- consume: rejection / atomicity -------------------------------------


def test_consume_rejects_over_allocation(service: QuotaService) -> None:
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]
    allocation = service.get_status(member.id).windows[WindowType.FIVE_HOUR].allocation_units

    result = service.consume(member.id, WindowType.FIVE_HOUR, allocation + 1, "over-alloc-key")

    assert result.accepted is False
    assert result.reason == "insufficient_quota"
    assert result.remaining_units == allocation


def test_consume_atomicity_rejected_consume_does_not_mutate_state(
    service: QuotaService, db_path: Path
) -> None:
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]
    allocation = service.get_status(member.id).windows[WindowType.FIVE_HOUR].allocation_units

    before = service.get_status(member.id).windows[WindowType.FIVE_HOUR]

    result = service.consume(member.id, WindowType.FIVE_HOUR, allocation + 1, "will-fail")
    assert result.accepted is False

    after = service.get_status(member.id).windows[WindowType.FIVE_HOUR]
    assert after.used_units == before.used_units == 0
    assert after.remaining_units == before.remaining_units == allocation

    # No usage_records row should have been written for the rejected attempt.
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM usage_records WHERE idempotency_key = ?", ("will-fail",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_consume_no_partial_consumption(service: QuotaService) -> None:
    """A rejected consume must not partially deduct - it's all or nothing."""
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]
    allocation = service.get_status(member.id).windows[WindowType.WEEKLY].allocation_units

    result = service.consume(member.id, WindowType.WEEKLY, allocation * 2, "too-much")
    assert result.accepted is False

    status = service.get_status(member.id).windows[WindowType.WEEKLY]
    assert status.used_units == 0


# --- consume: idempotency ------------------------------------------------


def test_consume_idempotent_same_key_consumes_once(service: QuotaService) -> None:
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]

    first = service.consume(member.id, WindowType.FIVE_HOUR, 100, "req-1")
    second = service.consume(member.id, WindowType.FIVE_HOUR, 100, "req-1")

    assert first.accepted is True
    assert first.replayed is False
    assert second.accepted is True
    assert second.replayed is True
    assert first.remaining_units == second.remaining_units

    status = service.get_status(member.id).windows[WindowType.FIVE_HOUR]
    assert status.used_units == 100  # consumed once, not twice


def test_consume_idempotency_conflict_raises_on_mismatched_params(service: QuotaService) -> None:
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]

    service.consume(member.id, WindowType.FIVE_HOUR, 100, "shared-key")

    with pytest.raises(IdempotencyConflictError):
        service.consume(member.id, WindowType.FIVE_HOUR, 200, "shared-key")


# --- window independence -------------------------------------------------


def test_five_hour_and_weekly_windows_track_independently(service: QuotaService) -> None:
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]

    service.consume(member.id, WindowType.FIVE_HOUR, 500, "fh-1")

    status = service.get_status(member.id)
    five_hour = status.windows[WindowType.FIVE_HOUR]
    weekly = status.windows[WindowType.WEEKLY]

    assert five_hour.used_units == 500
    assert weekly.used_units == 0
    assert weekly.remaining_units == weekly.allocation_units


# --- get_status ------------------------------------------------------------


def test_get_status_reflects_multiple_consumes(service: QuotaService) -> None:
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]

    service.consume(member.id, WindowType.FIVE_HOUR, 100, "k1")
    service.consume(member.id, WindowType.FIVE_HOUR, 250, "k2")
    service.consume(member.id, WindowType.WEEKLY, 1000, "k3")

    status = service.get_status(member.id)
    five_hour = status.windows[WindowType.FIVE_HOUR]
    weekly = status.windows[WindowType.WEEKLY]

    assert five_hour.used_units == 350
    assert five_hour.remaining_units == five_hour.allocation_units - 350
    assert weekly.used_units == 1000
    assert weekly.remaining_units == weekly.allocation_units - 1000


def test_get_status_unknown_member_raises(service: QuotaService) -> None:
    with pytest.raises(MemberNotFoundError):
        service.get_status("does-not-exist")


def test_check_quota_unknown_member_raises(service: QuotaService) -> None:
    with pytest.raises(QuotaWindowNotFoundError):
        service.check_quota("does-not-exist", WindowType.FIVE_HOUR, 10)


def test_check_quota_is_read_only(service: QuotaService) -> None:
    pool = service.create_pool("Solo", ["Alice"])
    member = service.list_members(pool.id)[0]

    result = service.check_quota(member.id, WindowType.FIVE_HOUR, 50)
    assert result.allowed is True

    status = service.get_status(member.id).windows[WindowType.FIVE_HOUR]
    assert status.used_units == 0  # checking must not consume


# --- concurrency: real locking, not read-modify-write --------------------


def test_consume_concurrent_calls_never_oversell(db_path: Path) -> None:
    service = QuotaService(uow_factory=lambda: SqliteUnitOfWork(db_path))
    pool = service.create_pool("Concurrency Pool", ["Solo"])
    member = service.list_members(pool.id)[0]
    allocation = service.get_status(member.id).windows[WindowType.FIVE_HOUR].allocation_units

    amount_each = 100
    max_possible_successes = allocation // amount_each
    attempts = max_possible_successes * 2  # exactly double the capacity

    def attempt(i: int):
        return service.consume(member.id, WindowType.FIVE_HOUR, amount_each, f"concurrent-{i}")

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt, i) for i in range(attempts)]
        for future in as_completed(futures):
            results.append(future.result())

    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]

    assert len(accepted) == max_possible_successes
    assert len(rejected) == attempts - max_possible_successes

    status = service.get_status(member.id).windows[WindowType.FIVE_HOUR]
    assert status.used_units == max_possible_successes * amount_each
    assert status.remaining_units == allocation - max_possible_successes * amount_each


def test_consume_concurrent_same_idempotency_key_consumes_once(db_path: Path) -> None:
    service = QuotaService(uow_factory=lambda: SqliteUnitOfWork(db_path))
    pool = service.create_pool("Concurrency Pool", ["Solo"])
    member = service.list_members(pool.id)[0]
    allocation = service.get_status(member.id).windows[WindowType.FIVE_HOUR].allocation_units

    def attempt(_: int):
        return service.consume(member.id, WindowType.FIVE_HOUR, allocation, "same-key-everywhere")

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt, i) for i in range(20)]
        for future in as_completed(futures):
            results.append(future.result())

    assert all(r.accepted for r in results)
    assert all(r.remaining_units == 0 for r in results)

    status = service.get_status(member.id).windows[WindowType.FIVE_HOUR]
    assert status.used_units == allocation  # consumed exactly once despite 20 concurrent calls

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM usage_records WHERE idempotency_key = ?",
            ("same-key-everywhere",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
