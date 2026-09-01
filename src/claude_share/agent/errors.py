"""Errors specific to the local agent/identity layer.

Distinct from `domain/errors.py`: these are about the local config file
and CLI-session state ("is this machine logged in yet?"), not about
domain invariants, so they don't belong in the domain layer - a future
server-side caller (Milestone 5) won't have a local config file at all.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for local-agent-layer errors."""


class NotLoggedInError(AgentError):
    def __init__(self) -> None:
        super().__init__(
            "This machine is not logged in yet. Run `claude-share login "
            "--user-id <user_id> --device-name <name>` first."
        )


class RemoteRequestError(AgentError):
    """Raised by `agent.remote_client` (Milestone 5) for any non-2xx
    response from a central server - both auth failures (401/403, no local
    equivalent before Milestone 5) and ordinary domain-rule rejections the
    local services would have raised as a `DomainError` (404/409/400).
    Deliberately not split into per-status-code subclasses: nothing in this
    codebase catches a *specific* `DomainError` subtype from a service call
    (every caller lets it bubble up to `cli/main.py:main()`'s blanket
    `except (DomainError, AgentError)`), so collapsing the server's status
    code + `detail` message into one exception type loses no behavior
    while keeping the remote client's error handling simple. See
    docs/architecture.md ("Milestone 5 - remote client mode")."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
