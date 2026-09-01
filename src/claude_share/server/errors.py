"""Domain error -> HTTP status code mapping for the Milestone 5 server.

The one place that translates `domain/errors.py` exceptions into HTTP
responses, so `routes.py` never has to think about status codes - it just
calls the application service and lets a `DomainError` propagate; a
FastAPI exception handler (registered in `app.py`) catches it here and
turns it into the right response. No business rule lives in this mapping -
it only decides which HTTP status best represents an error the
application layer already fully explains.

Postgres `deadlock_detected` (SQLSTATE 40P01) is handled separately in
`app.py` - see `DEADLOCK_DETECTED_DETAIL` below.
"""

from __future__ import annotations

from claude_share.domain.errors import (
    CapacityGrantNotFoundError,
    CapacityRequestNotFoundError,
    DomainError,
    GrantNotActiveError,
    IdempotencyConflictError,
    InsufficientQuotaError,
    InsufficientSourceCapacityError,
    InvalidCapacityRequestError,
    InvalidPoolConfigurationError,
    MemberNotFoundError,
    MemberNotInPoolError,
    MemberNotOwnedByUserError,
    NotAuthorizedError,
    PoolNotFoundError,
    QuotaWindowNotFoundError,
    RequestNotPendingError,
    UserNotFoundError,
)

#: Returned when Postgres aborts a transaction with SQLSTATE 40P01
#: (deadlock_detected) - a transient advisory-lock contention outcome,
#: not a data-integrity failure. The transaction was rolled back by Postgres;
#: the client may safely retry the same request.
DEADLOCK_DETECTED_DETAIL = (
    "Concurrent lock contention caused a deadlock (deadlock_detected). "
    "The transaction was rolled back safely; please retry this request."
)

#: 404 - the referenced entity simply doesn't exist.
_NOT_FOUND = (
    MemberNotFoundError,
    QuotaWindowNotFoundError,
    PoolNotFoundError,
    CapacityRequestNotFoundError,
    CapacityGrantNotFoundError,
    UserNotFoundError,
)

#: 400 - the request's own parameters are invalid/inconsistent.
_BAD_REQUEST = (
    InvalidCapacityRequestError,
    InvalidPoolConfigurationError,
    MemberNotInPoolError,
)

#: 403 - correctly identified, but not allowed to perform this action.
_FORBIDDEN = (
    NotAuthorizedError,
    MemberNotOwnedByUserError,
)

#: 409 - the request is well-formed but conflicts with the current state
#: (insufficient balance, wrong status for this transition, reused key).
_CONFLICT = (
    InsufficientQuotaError,
    InsufficientSourceCapacityError,
    IdempotencyConflictError,
    RequestNotPendingError,
    GrantNotActiveError,
)


def status_code_for(exc: DomainError) -> int:
    if isinstance(exc, _NOT_FOUND):
        return 404
    if isinstance(exc, _FORBIDDEN):
        return 403
    if isinstance(exc, _CONFLICT):
        return 409
    if isinstance(exc, _BAD_REQUEST):
        return 400
    return 400  # pragma: no cover - safety net for any future DomainError subclass
