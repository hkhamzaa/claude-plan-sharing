"""Domain-level exceptions.

These are the only errors the application layer should need to translate
into user-facing messages. Nothing here knows about SQLite, HTTP, or Claude.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-level errors."""


class InvalidPoolConfigurationError(DomainError):
    """Raised when a pool cannot be constructed with the given parameters."""


class MemberNotFoundError(DomainError):
    def __init__(self, member_id: str) -> None:
        self.member_id = member_id
        super().__init__(f"Member {member_id!r} not found.")


class QuotaWindowNotFoundError(DomainError):
    def __init__(self, member_id: str, window_type: str) -> None:
        self.member_id = member_id
        self.window_type = window_type
        super().__init__(f"No {window_type!r} window found for member {member_id!r}.")


class InsufficientQuotaError(DomainError):
    """Raised when a consume() would exceed a member's remaining quota."""

    def __init__(self, member_id: str, window_type: str, requested: int, remaining: int) -> None:
        self.member_id = member_id
        self.window_type = window_type
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"Member {member_id!r} requested {requested} unit(s) in window "
            f"{window_type!r} but only {remaining} remain."
        )


class IdempotencyConflictError(DomainError):
    """Raised when an idempotency key is reused with different call parameters."""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Idempotency key {idempotency_key!r} was already used with different "
            f"member/window/amount parameters."
        )


class PoolNotFoundError(DomainError):
    def __init__(self, pool_id: str) -> None:
        self.pool_id = pool_id
        super().__init__(f"Pool {pool_id!r} not found.")


class InvalidCapacityRequestError(DomainError):
    """Raised when a capacity request's own parameters are invalid or inconsistent."""


class CapacityRequestNotFoundError(DomainError):
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"Capacity request {request_id!r} not found.")


class CapacityGrantNotFoundError(DomainError):
    def __init__(self, grant_id: str) -> None:
        self.grant_id = grant_id
        super().__init__(f"Capacity grant {grant_id!r} not found.")


class RequestNotPendingError(DomainError):
    def __init__(self, request_id: str, status: str) -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(f"Capacity request {request_id!r} is {status!r}, not pending.")


class GrantNotActiveError(DomainError):
    def __init__(self, grant_id: str, status: str) -> None:
        self.grant_id = grant_id
        self.status = status
        super().__init__(f"Capacity grant {grant_id!r} is {status!r}, not active.")


class NotAuthorizedError(DomainError):
    """Raised when a member attempts an action reserved for a different member."""

    def __init__(self, member_id: str, action: str) -> None:
        self.member_id = member_id
        self.action = action
        super().__init__(f"Member {member_id!r} is not authorized to perform {action!r}.")


class UserNotFoundError(DomainError):
    """Raised when a user_id doesn't correspond to any existing Member."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        super().__init__(f"No member found with user_id {user_id!r}.")


class MemberNotInPoolError(DomainError):
    def __init__(self, member_id: str, pool_id: str) -> None:
        self.member_id = member_id
        self.pool_id = pool_id
        super().__init__(f"Member {member_id!r} does not belong to pool {pool_id!r}.")


class MemberNotOwnedByUserError(DomainError):
    def __init__(self, member_id: str, user_id: str) -> None:
        self.member_id = member_id
        self.user_id = user_id
        super().__init__(f"Member {member_id!r} does not belong to user_id {user_id!r}.")


class InsufficientSourceCapacityError(DomainError):
    """Raised when approving a request would over-commit the source's capacity."""

    def __init__(
        self,
        source_member_id: str,
        window_type: str,
        capacity_type: str,
        requested: int,
        available: int,
    ) -> None:
        self.source_member_id = source_member_id
        self.window_type = window_type
        self.capacity_type = capacity_type
        self.requested = requested
        self.available = available
        super().__init__(
            f"Member {source_member_id!r} cannot commit {requested} unit(s) of "
            f"{capacity_type!r} capacity in window {window_type!r}: only {available} "
            f"unencumbered unit(s) available."
        )
