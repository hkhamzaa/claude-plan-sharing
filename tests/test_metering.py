from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_share.integrations.claude_code.metering import (
    INPUT_TOKEN_WEIGHT,
    OUTPUT_TOKEN_WEIGHT,
    TOKEN_WEIGHT_SCALE,
    TurnVerification,
    derive_idempotency_key,
    read_current_turn_usage,
    tokens_to_units,
)


def _write_transcript(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _assistant_usage_entry(
    *,
    uuid: str,
    parent_uuid: str,
    text: str,
    input_tokens: int,
    output_tokens: int,
) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "message": {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    }


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "expected_units"),
    [
        (0, 0, 0),
        (1000, 0, 1),
        (0, 200, 1),
        (1000, 200, 2),  # (1000*1 + 200*5) / 1000 = 2
        (10_000, 2_000, 20),  # (10000 + 10000) / 1000
        (500, 100, 1),  # 1000 weighted -> ceil to 1
        (999, 0, 1),
        (0, 199, 1),  # 995 weighted -> ceil to 1
        (0, 200, 1),  # exactly 1000 weighted
        (0, 201, 2),
    ],
)
def test_tokens_to_units_weighting(input_tokens: int, output_tokens: int, expected_units: int) -> None:
    assert tokens_to_units(input_tokens, output_tokens) == expected_units


def test_tokens_to_units_uses_documented_weights() -> None:
    raw = 3000 * INPUT_TOKEN_WEIGHT + 400 * OUTPUT_TOKEN_WEIGHT
    assert raw == 5000
    assert tokens_to_units(3000, 400) == (raw + TOKEN_WEIGHT_SCALE - 1) // TOKEN_WEIGHT_SCALE


def test_tokens_to_units_rejects_negative() -> None:
    with pytest.raises(ValueError):
        tokens_to_units(-1, 0)
    with pytest.raises(ValueError):
        tokens_to_units(0, -5)


def test_derive_idempotency_key_prefers_prompt_id() -> None:
    assert derive_idempotency_key("sess-1", "prompt-uuid", 3) == "claude-code-stop:prompt-uuid"


def test_derive_idempotency_key_falls_back_to_session_and_turn_index() -> None:
    assert derive_idempotency_key("sess-1", None, 7) == "claude-code-stop:sess-1:7"


def test_read_current_turn_usage_turn_index_with_interleaved_entries(tmp_path: Path) -> None:
    """Regression: turn_index must count only assistant-usage entries.

    Non-assistant lines and assistant lines without usage (e.g. mid-turn
    tool_use) sit between the two billable assistant turns. The old
    ``turn_index - offset`` formula mixed all-entry offsets with a
    filtered forward count and produced wrong fallback idempotency keys.
    """
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            {"type": "user", "uuid": "user-1", "message": {"content": "first prompt"}},
            _assistant_usage_entry(
                uuid="asst-1",
                parent_uuid="user-1",
                text="first response",
                input_tokens=100,
                output_tokens=10,
            ),
            {
                "type": "assistant",
                "uuid": "asst-tool",
                "parentUuid": "asst-1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "uuid": "user-tool",
                "parentUuid": "asst-tool",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": "file.txt",
                        }
                    ]
                },
            },
            {"type": "user", "uuid": "user-2", "parentUuid": "user-tool", "message": {"content": "second prompt"}},
            _assistant_usage_entry(
                uuid="asst-2",
                parent_uuid="user-2",
                text="second response",
                input_tokens=200,
                output_tokens=20,
            ),
        ],
    )

    first = read_current_turn_usage(
        transcript,
        TurnVerification(last_assistant_message="first response"),
    )
    second = read_current_turn_usage(
        transcript,
        TurnVerification(last_assistant_message="second response"),
    )

    assert first is not None
    assert second is not None
    assert first.turn_index == 0
    assert second.turn_index == 1
    assert derive_idempotency_key("sess-1", None, first.turn_index) == "claude-code-stop:sess-1:0"
    assert derive_idempotency_key("sess-1", None, second.turn_index) == "claude-code-stop:sess-1:1"
