"""Claude Code `UserPromptSubmit` hook: check quota before a prompt runs.

## The verified integration mechanism

Claude Code's `UserPromptSubmit` hook fires once per prompt, before the
prompt reaches Claude. It's configured as a "command" hook in
`.claude/settings.json` (project) or `~/.claude/settings.json` (user) -
see `claude_share.integrations.claude_code.settings` for the installer.
The hook process:

  - receives a JSON payload on stdin (documented fields include at least
    `session_id`, `cwd`, `hook_event_name`, and the submitted prompt text)
  - signals its decision via exit code / stdout / stderr:
      - exit 2 blocks the prompt; the reason shown to the user comes from
        stderr text (or a `decision: "block"` JSON object with a `reason`
        field, which this module does not use - plain stderr text is
        simpler and sufficient here)
      - exit 0 allows the prompt through; plain stdout text on exit 0 is
        added as context alongside the prompt (used here for the
        low-quota warning)
  - has a default timeout of 30 seconds for this specific event - the
    hook must return quickly

This module deliberately does not depend on the VALUE of any stdin field
for its quota decision - see `_read_stdin_event()` below. It reads and
parses stdin only as good hook hygiene (fully draining it), not because
anything here inspects the prompt, the session, or the cwd.

## Pre-check only — real metering happens in the Stop hook

The upcoming prompt's token cost is unknowable before it runs. This hook
therefore checks only whether the member has **any** remaining guaranteed
FIVE_HOUR capacity right now (`remaining_units > 0`), not whether a
specific placeholder amount would fit. Actual consumption is recorded
after the turn completes by the companion `Stop` hook
(`stop_hook.py`), which reads Claude Code's reported token counts from
the session transcript and calls `QuotaService.consume()`.

## Fail-open error handling

Any unexpected internal error (local DB unreachable, corrupted config,
any other exception) results in exit 0 - allowing the prompt through -
never exit 2. A quota-management tool that crashes and permanently locks
someone out of Claude Code is a far worse failure mode than occasionally
letting a prompt through unchecked. Errors are best-effort logged to a
local file for debugging (see `_log_error()`), but logging failures are
themselves swallowed - nothing in this module is allowed to block a
prompt because of a bug here.

## Strictly opt-in

If this machine has no local identity configured (no `login`/`join` has
been run - see Milestone 3), this hook exits 0 with no output every time.
Someone who hasn't set up claude-share must see zero difference in their
Claude Code experience.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from claude_share.agent.identity import load_local_identity
from claude_share.domain.models import TOTAL_ALLOCATION_BPS, WindowType
from claude_share.integrations.claude_code._common import (
    log_hook_error,
    read_stdin_event,
    resolve_config_path,
    resolve_db_path,
)

#: Branding line shown at the top of both the warning and block messages.
BRAND_NAME = "Claude Share"

#: Warn (but still allow) once a member's remaining guaranteed FIVE_HOUR
#: capacity drops below this fraction of their own guaranteed ceiling.
#: 0.20 was chosen as a reasonable "you're getting close" signal: large
#: enough to give advance notice before the hard block (roughly the last
#: fifth of a window's capacity), small enough not to nag through the back
#: half of a window. Not user-configurable in this milestone.
WARNING_THRESHOLD_FRACTION: float = 0.20

HOOK_NAME = "UserPromptSubmit hook"


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _render_message(headline: str, guaranteed_units: int, used_units: int, reset_at: datetime, now: datetime) -> str:
    """Renders the spec's example format, e.g.:

        Claude Share
        Allocation exhausted.
        Used: 25.0% / 25%
        Reset: 1h 32m

    Both percentages are expressed as a share of the POOL's total (out of
    TOTAL_ALLOCATION_BPS): the left number is how much of the pool this
    member has personally used, the right is the size of their own
    guaranteed slice of the pool. The two become equal exactly when the
    member has used their entire guaranteed capacity.
    """
    used_pct = used_units / TOTAL_ALLOCATION_BPS * 100
    share_pct = guaranteed_units / TOTAL_ALLOCATION_BPS * 100
    return (
        f"{BRAND_NAME}\n"
        f"{headline}\n"
        f"Used: {used_pct:.1f}% / {share_pct:.0f}%\n"
        f"Reset: {_format_duration(reset_at - now)}"
    )


def _run() -> int:
    read_stdin_event()  # drained, unused - see module docstring

    identity = load_local_identity(resolve_config_path())
    if identity is None or identity.pool_id is None or identity.member_id is None:
        return 0  # strictly opt-in: nothing configured, do not interfere

    # Deferred until we know we actually need them, so the (common, until
    # someone opts in) "not configured" path above never touches SQLite or
    # the application layer at all.
    from claude_share.application.capacity_service import CapacityService
    from claude_share.application.quota_service import QuotaService
    from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork

    uow_factory = lambda: SqliteUnitOfWork(resolve_db_path())
    quota_service = QuotaService(uow_factory=uow_factory)
    capacity_service = CapacityService(uow_factory=uow_factory)

    status = quota_service.get_status(identity.member_id)
    window_status = status.windows[WindowType.FIVE_HOUR]
    effective = capacity_service.get_effective_capacity(identity.member_id, WindowType.FIVE_HOUR)

    guaranteed_units = effective.guaranteed_units
    used_units = window_status.used_units
    remaining_units = max(guaranteed_units - used_units, 0)
    now = datetime.now(timezone.utc)

    if remaining_units <= 0:
        message = _render_message("Allocation exhausted.", guaranteed_units, used_units, window_status.reset_at, now)
        print(message, file=sys.stderr)
        return 2

    if guaranteed_units > 0 and (remaining_units / guaranteed_units) < WARNING_THRESHOLD_FRACTION:
        message = _render_message("Quota running low.", guaranteed_units, used_units, window_status.reset_at, now)
        print(message)

    return 0


def main() -> int:
    """Entry point invoked by Claude Code as the UserPromptSubmit hook.

    Fails OPEN: any unexpected exception is logged locally and results in
    exit 0 (prompt allowed through), never exit 2. See module docstring
    ("Fail-open error handling").
    """
    try:
        return _run()
    except Exception as exc:
        log_hook_error(HOOK_NAME, exc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
