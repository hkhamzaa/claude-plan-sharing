from __future__ import annotations

import pytest

from claude_share.domain.allocation import compute_equal_allocations_bps
from claude_share.domain.errors import InvalidPoolConfigurationError
from claude_share.domain.models import TOTAL_ALLOCATION_BPS


@pytest.mark.parametrize("member_count", [1, 2, 3, 4, 5, 7, 10])
def test_allocations_sum_to_total(member_count: int) -> None:
    shares = compute_equal_allocations_bps(member_count)
    assert len(shares) == member_count
    assert sum(shares) == TOTAL_ALLOCATION_BPS


def test_allocations_are_as_equal_as_possible() -> None:
    shares = compute_equal_allocations_bps(3)
    assert max(shares) - min(shares) <= 1


def test_allocations_deterministic_rounding_example() -> None:
    # 10,000 / 3 = 3333.33... -> first member(s) absorb the remainder.
    assert compute_equal_allocations_bps(3) == [3334, 3333, 3333]


def test_allocations_even_split() -> None:
    assert compute_equal_allocations_bps(4) == [2500, 2500, 2500, 2500]


def test_allocations_deterministic_across_calls() -> None:
    assert compute_equal_allocations_bps(7) == compute_equal_allocations_bps(7)


def test_zero_members_rejected() -> None:
    with pytest.raises(InvalidPoolConfigurationError):
        compute_equal_allocations_bps(0)


def test_negative_members_rejected() -> None:
    with pytest.raises(InvalidPoolConfigurationError):
        compute_equal_allocations_bps(-1)
