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
