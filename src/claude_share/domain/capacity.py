"""Pure arithmetic for grant-derived capacity figures (Milestone 2).

Like `allocation.py`, this module has no I/O: it takes already-fetched
numbers/entities and computes derived values. Fetching the grants
themselves (deciding what counts as "active", summing them up) is an
application-layer concern (see `application/capacity_queries.py`) because
it requires repository access; this module is where the resulting
arithmetic lives so it's independently testable and reused identically by
`QuotaService.consume()` and `CapacityService`.
"""

from __future__ import annotations

from datetime import datetime

from claude_share.domain.models import CapacityGrant, GrantStatus


def compute_guaranteed_units(base_allocation: int, solid_sent: int, solid_received: int) -> int:
    """A member's own ceiling: base allocation, minus what they've permanently
    given away via SOLID grants, plus what they've permanently received."""
    return base_allocation - solid_sent + solid_received


def compute_potential_units(guaranteed_units: int, shared_borrowed_potential: int) -> int:
    """Upper bound on what a member might be able to consume: guaranteed
    capacity plus the (unguaranteed) ceiling of SHARED grants they hold as
    recipient. Not a promise - see `EffectiveCapacity.potential_units`."""
    return guaranteed_units + shared_borrowed_potential


def is_grant_usable(grant: CapacityGrant, now: datetime) -> bool:
    """Whether a grant can currently back a consume() or capacity computation.

    A grant is usable only while ACTIVE and before its `expires_at`. Expiry
    is checked here against wall-clock time rather than via a background
    sweep - see docs/architecture.md ("Grant expiration policy").
    """
    return grant.status is GrantStatus.ACTIVE and grant.expires_at > now
