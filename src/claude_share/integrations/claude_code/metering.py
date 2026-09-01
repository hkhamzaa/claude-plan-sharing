"""Token-count-based usage metering for Claude Code Stop hooks.

Token counts are read from the session transcript JSONL that Claude Code
writes. The Stop hook payload itself does not carry usage fields - only a
``transcript_path`` pointer plus turn-identifying signals (``prompt_id``,
``last_assistant_message``) used to reject stale prior-turn entries when
the current turn's line is still in flight.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Per-input-token weight in the abstract-units formula. Chosen as 1 so
#: input tokens form the baseline unit of account.
INPUT_TOKEN_WEIGHT: int = 1

#: Per-output-token weight. Anthropic's public API pricing for Sonnet-class
#: models charges output tokens at roughly five times the input-token rate
#: (e.g. $3/M input vs $15/M output), so 5 mirrors that cost ratio without
#: tying units to dollars. Adjustable without an architecture change.
OUTPUT_TOKEN_WEIGHT: int = 5

#: Divisor applied after the weighted sum so a typical turn (tens of
#: thousands of tokens) consumes tens of abstract units rather than tens of
#: thousands, keeping numbers comparable to Milestone 1's pool allocations.
TOKEN_WEIGHT_SCALE: int = 1000

#: How long to poll for the just-completed assistant entry to appear in the
#: transcript file. Claude Code documents that transcript writes are
#: asynchronous and may lag the Stop hook invocation.
TRANSCRIPT_POLL_MAX_WAIT_SECONDS: float = 8.0

#: Initial poll interval; doubles on each retry up to 1 second.
TRANSCRIPT_POLL_INITIAL_INTERVAL_SECONDS: float = 0.05


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """Measured token usage for one completed assistant turn."""

    input_tokens: int
    output_tokens: int
    turn_index: int
    message_id: str | None
    resolved_prompt_id: str | None


@dataclass(frozen=True, slots=True)
class TurnVerification:
    """Signals from the Stop hook payload that identify the current turn.

    At least one must be set; otherwise the transcript cannot be matched to
  the just-completed turn and metering fails open.
    """

    prompt_id: str | None = None
    last_assistant_message: str | None = None

    def __post_init__(self) -> None:
        if not self.prompt_id and not _normalized_message(self.last_assistant_message):
            raise ValueError("TurnVerification requires prompt_id and/or last_assistant_message")


def tokens_to_units(input_tokens: int, output_tokens: int) -> int:
    """Convert measured token counts into abstract quota units.

    Formula (before scaling):
        (input_tokens * INPUT_TOKEN_WEIGHT) + (output_tokens * OUTPUT_TOKEN_WEIGHT)

    The result is scaled down by TOKEN_WEIGHT_SCALE and rounded up so every
    measured turn with any tokens costs at least one unit.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if input_tokens == 0 and output_tokens == 0:
        return 0

    weighted = input_tokens * INPUT_TOKEN_WEIGHT + output_tokens * OUTPUT_TOKEN_WEIGHT
    return max(1, (weighted + TOKEN_WEIGHT_SCALE - 1) // TOKEN_WEIGHT_SCALE)


def derive_idempotency_key(session_id: str, prompt_id: str | None, turn_index: int) -> str:
    """Stable key so a Stop hook re-invocation cannot double-charge a turn.

  Primary: ``prompt_id`` from the Stop payload (documented common field,
  v2.1.196+), which uniquely identifies the user prompt for the turn.

  Fallback: ``session_id`` plus the zero-based index of the last assistant
  message in the transcript (count of ``type: assistant`` entries minus one).
  Used only when ``prompt_id`` is absent (older Claude Code versions).
    """
    if prompt_id:
        return f"claude-code-stop:{prompt_id}"
    return f"claude-code-stop:{session_id}:{turn_index}"


def _normalized_message(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _entry_prompt_id(entry: dict) -> str | None:
    for key in ("promptId", "prompt_id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_entry_prompt_id(entry: dict, by_uuid: dict[str, dict]) -> str | None:
    """Walk parentUuid back until an ancestor carries promptId.

    Transcript user entries are stamped with ``promptId`` matching the Stop
    hook's ``prompt_id``. Assistant entries inherit it through the causal
    chain (documented community format; same approach as Claude Code tooling
    that correlates Stop hooks to transcript turns).
    """
    current: dict | None = entry
    visited: set[str] = set()

    while current is not None:
        prompt_id = _entry_prompt_id(current)
        if prompt_id is not None:
            return prompt_id

        parent_uuid = current.get("parentUuid")
        if not isinstance(parent_uuid, str) or not parent_uuid or parent_uuid in visited:
            return None

        visited.add(parent_uuid)
        current = by_uuid.get(parent_uuid)

    return None


def _extract_assistant_text(entry: dict) -> str:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"].strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _assistant_message_matches(entry: dict, expected_message: str) -> bool:
    return _extract_assistant_text(entry) == expected_message


def _parse_assistant_usage(entry: dict) -> tuple[int, int, str | None] | None:
    if entry.get("type") != "assistant":
        return None
    if entry.get("isSidechain") is True:
        return None

    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None

    message_id = message.get("id")
    if message_id is not None and not isinstance(message_id, str):
        message_id = None
    return input_tokens, output_tokens, message_id


def _load_transcript_entries(transcript_path: Path) -> tuple[list[dict], dict[str, dict]]:
    if not transcript_path.is_file():
        return [], {}

    entries: list[dict] = []
    by_uuid: dict[str, dict] = {}

    try:
        with transcript_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # Partial / in-flight line — skip without treating older
                    # complete lines as the current turn.
                    continue
                if not isinstance(entry, dict):
                    continue
                entries.append(entry)
                uuid = entry.get("uuid")
                if isinstance(uuid, str) and uuid:
                    by_uuid[uuid] = entry
    except OSError:
        return [], {}

    return entries, by_uuid


def _entry_matches_current_turn(
    entry: dict,
    *,
    by_uuid: dict[str, dict],
    verification: TurnVerification,
) -> bool:
    expected_prompt_id = verification.prompt_id
    expected_message = _normalized_message(verification.last_assistant_message)

    if expected_prompt_id is not None:
        resolved_prompt_id = _resolve_entry_prompt_id(entry, by_uuid)
        if resolved_prompt_id != expected_prompt_id:
            return False

    if expected_message is not None and not _assistant_message_matches(entry, expected_message):
        return False

    return True


def read_current_turn_usage(
    transcript_path: Path,
    verification: TurnVerification,
) -> TurnUsage | None:
    """Return usage for the assistant entry that belongs to the current turn.

    Scans assistant entries from newest to oldest and returns the first that
    (a) has parseable ``message.usage`` and (b) matches ``verification``.
    A valid-but-stale prior-turn entry is rejected, so a poll loop keeps
    retrying until the current turn lands or times out.
    """
    entries, by_uuid = _load_transcript_entries(transcript_path)
    if not entries:
        return None

    usage_entries: list[tuple[dict, tuple[int, int, str | None]]] = []
    for entry in entries:
        parsed = _parse_assistant_usage(entry)
        if parsed is not None:
            usage_entries.append((entry, parsed))

    for turn_index in range(len(usage_entries) - 1, -1, -1):
        entry, parsed = usage_entries[turn_index]
        if not _entry_matches_current_turn(entry, by_uuid=by_uuid, verification=verification):
            continue

        input_tokens, output_tokens, message_id = parsed
        return TurnUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            turn_index=turn_index,
            message_id=message_id,
            resolved_prompt_id=_resolve_entry_prompt_id(entry, by_uuid),
        )

    return None


def read_last_assistant_usage(transcript_path: Path) -> TurnUsage | None:
    """Read the newest assistant usage without turn verification.

    Prefer ``read_current_turn_usage()`` for Stop-hook metering.
    """
    entries, by_uuid = _load_transcript_entries(transcript_path)
    turn_index = -1
    last_usage: TurnUsage | None = None

    for entry in entries:
        parsed = _parse_assistant_usage(entry)
        if parsed is None:
            continue
        turn_index += 1
        input_tokens, output_tokens, message_id = parsed
        last_usage = TurnUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            turn_index=turn_index,
            message_id=message_id,
            resolved_prompt_id=_resolve_entry_prompt_id(entry, by_uuid),
        )

    return last_usage


def wait_for_current_turn_usage(
    transcript_path: Path,
    verification: TurnVerification,
    *,
    max_wait_seconds: float = TRANSCRIPT_POLL_MAX_WAIT_SECONDS,
    initial_interval_seconds: float = TRANSCRIPT_POLL_INITIAL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> TurnUsage | None:
    """Poll until the current turn's assistant usage is readable and verified."""
    deadline = monotonic() + max_wait_seconds
    interval = initial_interval_seconds

    while True:
        usage = read_current_turn_usage(transcript_path, verification)
        if usage is not None:
            return usage

        remaining = deadline - monotonic()
        if remaining <= 0:
            return None

        sleep(min(interval, remaining))
        interval = min(interval * 2, 1.0)


def wait_for_last_assistant_usage(
    transcript_path: Path,
    *,
    max_wait_seconds: float = TRANSCRIPT_POLL_MAX_WAIT_SECONDS,
    initial_interval_seconds: float = TRANSCRIPT_POLL_INITIAL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> TurnUsage | None:
    """Backward-compatible wrapper without turn verification (tests only)."""
    deadline = monotonic() + max_wait_seconds
    interval = initial_interval_seconds

    while True:
        usage = read_last_assistant_usage(transcript_path)
        if usage is not None:
            return usage

        remaining = deadline - monotonic()
        if remaining <= 0:
            return None

        sleep(min(interval, remaining))
        interval = min(interval * 2, 1.0)
