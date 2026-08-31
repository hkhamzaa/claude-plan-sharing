"""Deterministic fixed-point basis-point allocation math.

This is pure arithmetic: given N members, split TOTAL_ALLOCATION_BPS
(10,000 bps == 100%) into N integer shares that sum to exactly
TOTAL_ALLOCATION_BPS, with no floating point anywhere.
"""

from __future__ import annotations

from claude_share.domain.errors import InvalidPoolConfigurationError
from claude_share.domain.models import TOTAL_ALLOCATION_BPS


def compute_equal_allocations_bps(member_count: int) -> list[int]:
    """Split TOTAL_ALLOCATION_BPS into `member_count` equal integer shares.

    Rounding is deterministic: when TOTAL_ALLOCATION_BPS does not divide
    evenly, the first `remainder` members (by input order) each receive one
    extra basis point. For example, member_count=3 -> [3334, 3333, 3333].

    The result always sums to exactly TOTAL_ALLOCATION_BPS.
    """
    if member_count <= 0:
        raise InvalidPoolConfigurationError(
            f"member_count must be a positive integer, got {member_count}"
        )

    base_share, remainder = divmod(TOTAL_ALLOCATION_BPS, member_count)
    shares = [base_share + 1 if index < remainder else base_share for index in range(member_count)]

    assert sum(shares) == TOTAL_ALLOCATION_BPS  # sanity check, always true by construction
    return shares
