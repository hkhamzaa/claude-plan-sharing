"""Application service implementing the Milestone 2 capacity-delegation
lifecycle: request -> approve/reject -> grant -> revoke, plus the
grant-aware effective-capacity read.

Follows the same `uow_factory` dependency-injection pattern as
`QuotaService` - see docs/architecture.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from claude_share.application.capacity_queries import active_grants_as_of, member_grant_summary
from claude_share.application.dto import EffectiveCapacity
from claude_share.application.ids import new_id
from claude_share.domain.capacity import compute_guaranteed_units, compute_potential_units
from claude_share.domain.errors import (
    CapacityGrantNotFoundError,
    CapacityRequestNotFoundError,
    GrantNotActiveError,
    InsufficientSourceCapacityError,
    InvalidCapacityRequestError,
    MemberNotFoundError,
    NotAuthorizedError,
    PoolNotFoundError,
    QuotaWindowNotFoundError,
    RequestNotPendingError,
)
from claude_share.domain.models import (
    CapacityGrant,
    CapacityRequest,
    CapacityType,
    GrantStatus,
    RequestStatus,
    WindowType,
)
from claude_share.domain.repository import UnitOfWork


class CapacityService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def request_capacity(
        self,
        pool_id: str,
        requester_member_id: str,
        target_member_id: str,
        window_type: WindowType,
        amount: int,
        type: CapacityType,
        message: str | None = None,
    ) -> CapacityRequest:
        """Create a PENDING request. Never moves or reserves any capacity."""
        if amount <= 0:
            raise ValueError("amount must be a positive integer")
        if requester_member_id == target_member_id:
            raise InvalidCapacityRequestError("requester_member_id and target_member_id must differ")

        now = datetime.now(timezone.utc)

        with self._uow_factory() as uow:
            pool = uow.pools.get(pool_id)
            if pool is None:
                uow.rollback()
                raise PoolNotFoundError(pool_id)

            requester = uow.members.get(requester_member_id)
            if requester is None:
                uow.rollback()
                raise MemberNotFoundError(requester_member_id)

            target = uow.members.get(target_member_id)
            if target is None:
                uow.rollback()
                raise MemberNotFoundError(target_member_id)

            if requester.pool_id != pool_id or target.pool_id != pool_id:
                uow.rollback()
                raise InvalidCapacityRequestError(
                    "requester_member_id and target_member_id must both belong to pool_id"
                )

            request = CapacityRequest(
                id=new_id(),
                pool_id=pool_id,
                requester_member_id=requester_member_id,
                target_member_id=target_member_id,
                window_type=window_type,
                amount=amount,
                type=type,
                status=RequestStatus.PENDING,
                created_at=now,
                message=message,
            )
            uow.requests.add(request)
            uow.commit()

        return request

    def approve_request(self, request_id: str, approving_member_id: str) -> CapacityGrant:
        """Approve a PENDING request, atomically checking and creating the grant.

        Only `request.target_member_id` (the capacity owner) may approve.
        SOLID requests are checked against the source's allocation minus
        other active SOLID grants they've already sent; SHARED requests are
        checked against the source's allocation minus other active SHARED
        grants they've already offered. See docs/architecture.md for why
        these two checks are independent of each other.
        """
        now = datetime.now(timezone.utc)

        with self._uow_factory() as uow:
            request = uow.requests.get(request_id)
            if request is None:
                uow.rollback()
                raise CapacityRequestNotFoundError(request_id)

            if request.status is not RequestStatus.PENDING:
                uow.rollback()
                raise RequestNotPendingError(request_id, request.status.value)

            if approving_member_id != request.target_member_id:
                uow.rollback()
                raise NotAuthorizedError(approving_member_id, "approve_request")

            source_window = uow.windows.get(request.target_member_id, request.window_type)
            if source_window is None:
                uow.rollback()
                raise QuotaWindowNotFoundError(request.target_member_id, request.window_type.value)

            source_summary = member_grant_summary(uow, request.target_member_id, request.window_type, now)

            if request.type is CapacityType.SOLID:
                unencumbered = source_window.allocation_units - source_summary.solid_sent
                if unencumbered < request.amount:
                    uow.rollback()
                    raise InsufficientSourceCapacityError(
                        request.target_member_id,
                        request.window_type.value,
                        CapacityType.SOLID.value,
                        request.amount,
                        unencumbered,
                    )
            else:
                available_to_offer = source_window.allocation_units - source_summary.shared_offered
                if available_to_offer < request.amount:
                    uow.rollback()
                    raise InsufficientSourceCapacityError(
                        request.target_member_id,
                        request.window_type.value,
                        CapacityType.SHARED.value,
                        request.amount,
                        available_to_offer,
                    )

            approved_request = replace(request, status=RequestStatus.APPROVED, approved_at=now)
            grant = CapacityGrant(
                id=new_id(),
                pool_id=request.pool_id,
                source_member_id=request.target_member_id,
                recipient_member_id=request.requester_member_id,
                window_type=request.window_type,
                amount=request.amount,
                type=request.type,
                status=GrantStatus.ACTIVE,
                created_at=now,
                activated_at=now,
                # Default expiration policy: the end of the source's CURRENT
                # quota window. See docs/architecture.md.
                expires_at=source_window.reset_at,
            )

            uow.requests.update(approved_request)
            uow.grants.add(grant)
            uow.commit()

        return grant

    def reject_request(self, request_id: str, rejecting_member_id: str) -> CapacityRequest:
        """Reject a PENDING request. Only `target_member_id` may reject."""
        with self._uow_factory() as uow:
            request = uow.requests.get(request_id)
            if request is None:
                uow.rollback()
                raise CapacityRequestNotFoundError(request_id)

            if request.status is not RequestStatus.PENDING:
                uow.rollback()
                raise RequestNotPendingError(request_id, request.status.value)

            if rejecting_member_id != request.target_member_id:
                uow.rollback()
                raise NotAuthorizedError(rejecting_member_id, "reject_request")

            rejected_request = replace(request, status=RequestStatus.REJECTED)
            uow.requests.update(rejected_request)
            uow.commit()

        return rejected_request

    def revoke_grant(self, grant_id: str, revoking_member_id: str) -> CapacityGrant:
        """Revoke an ACTIVE grant early. Only `source_member_id` may revoke.

        Restoring the source's guaranteed capacity (SOLID) or removing the
        recipient's access (SHARED) is automatic: both are computed live
        from ACTIVE grants only, so the moment this grant's status flips to
        REVOKED it stops contributing to either figure.
        """
        now = datetime.now(timezone.utc)

        with self._uow_factory() as uow:
            grant = uow.grants.get(grant_id)
            if grant is None:
                uow.rollback()
                raise CapacityGrantNotFoundError(grant_id)

            if grant.status is not GrantStatus.ACTIVE:
                uow.rollback()
                raise GrantNotActiveError(grant_id, grant.status.value)

            if revoking_member_id != grant.source_member_id:
                uow.rollback()
                raise NotAuthorizedError(revoking_member_id, "revoke_grant")

            revoked_grant = replace(grant, status=GrantStatus.REVOKED, revoked_at=now)
            uow.grants.update(revoked_grant)
            uow.commit()

        return revoked_grant

    def list_pending_requests_for_target(self, member_id: str) -> list[CapacityRequest]:
        """Pending capacity requests awaiting this member's approval."""
        with self._uow_factory() as uow:
            member = uow.members.get(member_id)
            if member is None:
                uow.rollback()
                raise MemberNotFoundError(member_id)
            requests = uow.requests.list_pending_by_target(member_id)
            uow.commit()
        return requests

    def list_active_grants(self, member_id: str) -> tuple[list[CapacityGrant], list[CapacityGrant]]:
        """Active grants this member has sent (as source) and received (as recipient)."""
        now = datetime.now(timezone.utc)
        sent: list[CapacityGrant] = []
        received: list[CapacityGrant] = []

        with self._uow_factory() as uow:
            member = uow.members.get(member_id)
            if member is None:
                uow.rollback()
                raise MemberNotFoundError(member_id)

            for window_type in WindowType:
                sent.extend(
                    active_grants_as_of(uow, uow.grants.list_by_source(member_id, window_type), now)
                )
                received.extend(
                    active_grants_as_of(uow, uow.grants.list_by_recipient(member_id, window_type), now)
                )
            uow.commit()

        return sent, received

    def get_effective_capacity(self, member_id: str, window_type: WindowType) -> EffectiveCapacity:
        """Grant-aware view of a member's capacity: base, guaranteed, and
        the (unguaranteed) potential ceiling from SHARED grants."""
        now = datetime.now(timezone.utc)

        with self._uow_factory() as uow:
            member = uow.members.get(member_id)
            if member is None:
                uow.rollback()
                raise MemberNotFoundError(member_id)

            window = uow.windows.get(member_id, window_type)
            if window is None:
                uow.rollback()
                raise QuotaWindowNotFoundError(member_id, window_type.value)

            summary = member_grant_summary(uow, member_id, window_type, now)
            uow.commit()

        guaranteed_units = compute_guaranteed_units(
            window.allocation_units, summary.solid_sent, summary.solid_received
        )
        potential_units = compute_potential_units(guaranteed_units, summary.shared_borrowed_potential)

        return EffectiveCapacity(
            member_id=member_id,
            window_type=window_type,
            base_allocation_units=window.allocation_units,
            solid_sent=summary.solid_sent,
            solid_received=summary.solid_received,
            guaranteed_units=guaranteed_units,
            shared_offered=summary.shared_offered,
            shared_borrowed_potential=summary.shared_borrowed_potential,
            potential_units=potential_units,
        )
