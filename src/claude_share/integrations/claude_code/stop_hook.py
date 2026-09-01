"""Claude Code `Stop` hook: meter real token usage after a turn completes.

## The verified integration mechanism

Claude Code's `Stop` hook fires once per turn when the main agent finishes
responding (confirmed against Anthropic's official hooks reference:
https://docs.anthropic.com/en/docs/claude-code/hooks — "Stop" section).
The hook process receives JSON on stdin with common fields including
`session_id`, `transcript_path`, and (v2.1.196+) `prompt_id`. The Stop
payload does **not** include token counts or a `usage` object — those live
in the session transcript JSONL that `transcript_path` points to. Each
`type: "assistant"` line carries `message.usage` with `input_tokens` and
`output_tokens` (same shape documented for PostToolUse `tool_response.usage`
in the hooks reference).

Because transcript writes are asynchronous, this hook polls
`transcript_path` with exponential backoff (see `metering.py`) until the
last assistant entry's usage is readable, or times out.

## Fail-open error handling

Any failure to read token data, compute units, or call `consume()` is
logged locally and results in exit 0 with zero consumption — never a
guessed fallback amount. Stop hooks cannot block Claude Code; their only
job is after-the-fact metering.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_share.agent.identity import load_local_identity
from claude_share.domain.models import WindowType
from claude_share.integrations.claude_code._common import (
    log_hook_error,
    read_stdin_event,
    resolve_config_path,
    resolve_db_path,
)
from claude_share.integrations.claude_code.metering import (
    TurnVerification,
    derive_idempotency_key,
    tokens_to_units,
    wait_for_current_turn_usage,
)

HOOK_NAME = "Stop hook"


def _resolve_transcript_path(event: dict) -> Path | None:
    env_override = os.environ.get("CLAUDE_SHARE_TRANSCRIPT_PATH")
    if env_override:
        return Path(env_override).expanduser()

    transcript = event.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        return Path(transcript).expanduser()
    return None


def _build_quota_service(identity):
    if identity.is_remote:
        from claude_share.agent.remote_client import build_remote_services

        quota_service, _, _ = build_remote_services(identity.server_url, identity.device_token)
        return quota_service

    from claude_share.application.quota_service import QuotaService
    from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork

    return QuotaService(uow_factory=lambda: SqliteUnitOfWork(resolve_db_path()))


def _run() -> int:
    event = read_stdin_event()

    identity = load_local_identity(resolve_config_path())
    if identity is None or identity.pool_id is None or identity.member_id is None:
        return 0

    transcript_path = _resolve_transcript_path(event)
    if transcript_path is None:
        return 0

    prompt_id = event.get("prompt_id")
    if prompt_id is not None and not isinstance(prompt_id, str):
        prompt_id = None

    last_assistant_message = event.get("last_assistant_message")
    if last_assistant_message is not None and not isinstance(last_assistant_message, str):
        last_assistant_message = None

    if not prompt_id and not (isinstance(last_assistant_message, str) and last_assistant_message.strip()):
        return 0

    try:
        verification = TurnVerification(
            prompt_id=prompt_id,
            last_assistant_message=last_assistant_message,
        )
    except ValueError:
        return 0

    usage = wait_for_current_turn_usage(transcript_path, verification)
    if usage is None:
        return 0

    amount = tokens_to_units(usage.input_tokens, usage.output_tokens)
    if amount <= 0:
        return 0

    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    idempotency_key = derive_idempotency_key(session_id, prompt_id, usage.turn_index)

    quota_service = _build_quota_service(identity)
    quota_service.consume(
        member_id=identity.member_id,
        window_type=WindowType.FIVE_HOUR,
        amount=amount,
        idempotency_key=idempotency_key,
    )
    return 0


def main() -> int:
    """Entry point invoked by Claude Code as the Stop hook."""
    try:
        return _run()
    except Exception as exc:
        log_hook_error(HOOK_NAME, exc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
