from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from claude_share.agent.identity import LocalIdentity, save_local_identity
from claude_share.application.quota_service import QuotaService
from claude_share.domain.models import WindowType
from claude_share.integrations.claude_code import hook, stop_hook
from claude_share.integrations.claude_code.metering import (
    TurnVerification,
    read_current_turn_usage,
    tokens_to_units,
    wait_for_current_turn_usage,
    wait_for_last_assistant_usage,
)
from claude_share.server.app import create_app


def _assistant_entry(
    input_tokens: int,
    output_tokens: int,
    *,
    uuid: str,
    parent_uuid: str,
    text: str = "assistant response",
    message_id: str | None = None,
) -> dict:
    message: dict = {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "model": "claude-sonnet-4-6",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    if message_id is not None:
        message["id"] = message_id
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "message": message,
    }


def _user_entry(
    content: str,
    *,
    uuid: str,
    prompt_id: str,
    parent_uuid: str | None = None,
) -> dict:
    entry = {
        "type": "user",
        "uuid": uuid,
        "promptId": prompt_id,
        "message": {"content": content},
    }
    if parent_uuid is not None:
        entry["parentUuid"] = parent_uuid
    return entry


def _turn_transcript(
  prompt_id: str,
  assistant_uuid: str,
  *,
  user_uuid: str = "user-1",
  parent_uuid: str | None = None,
  text: str,
  input_tokens: int,
  output_tokens: int,
) -> list[dict]:
    return [
        _user_entry("prompt", uuid=user_uuid, prompt_id=prompt_id, parent_uuid=parent_uuid),
        _assistant_entry(
            input_tokens,
            output_tokens,
            uuid=assistant_uuid,
            parent_uuid=user_uuid,
            text=text,
        ),
    ]


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _run_stop_hook(
    monkeypatch,
    config_path: Path,
    db_path: Path,
    capsys,
    *,
    stdin_payload: dict,
    transcript_path: Path | None = None,
) -> tuple[int, str, str]:
    monkeypatch.setenv("CLAUDE_SHARE_CONFIG", str(config_path))
    monkeypatch.setenv("CLAUDE_SHARE_DB", str(db_path))
    if transcript_path is not None:
        monkeypatch.setenv("CLAUDE_SHARE_TRANSCRIPT_PATH", str(transcript_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_payload)))
    exit_code = stop_hook.main()
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _make_identity(pool_id, member_id, user_id, **kwargs) -> LocalIdentity:
    return LocalIdentity(
        pool_id=pool_id,
        member_id=member_id,
        user_id=user_id,
        device_id="test-device",
        device_name="Test Device",
        **kwargs,
    )


def test_read_current_turn_usage_matches_prompt_id_chain(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            *_turn_transcript(
                "prompt-old",
                "asst-old",
                text="old response",
                input_tokens=1000,
                output_tokens=50,
            ),
            *_turn_transcript(
                "prompt-new",
                "asst-new",
                user_uuid="user-2",
                parent_uuid="asst-old",
                text="new response",
                input_tokens=2000,
                output_tokens=100,
            ),
        ],
    )

    usage = read_current_turn_usage(transcript, TurnVerification(prompt_id="prompt-new"))
    assert usage is not None
    assert usage.input_tokens == 2000
    assert usage.output_tokens == 100
    assert usage.turn_index == 1
    assert usage.resolved_prompt_id == "prompt-new"


def test_read_current_turn_usage_rejects_stale_last_entry(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        _turn_transcript(
            "prompt-old",
            "asst-old",
            text="old response",
            input_tokens=9000,
            output_tokens=9000,
        ),
    )

    usage = read_current_turn_usage(
        transcript,
        TurnVerification(prompt_id="prompt-new", last_assistant_message="new response"),
    )
    assert usage is None


def test_stop_hook_consumes_weighted_units_local(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys, tmp_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        _turn_transcript(
            "prompt-123",
            "asst-1",
            text="done",
            input_tokens=1000,
            output_tokens=200,
        ),
    )

    before = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units
    exit_code, _, _ = _run_stop_hook(
        monkeypatch,
        config_path,
        db_path,
        capsys,
        stdin_payload={
            "session_id": "sess-abc",
            "prompt_id": "prompt-123",
            "last_assistant_message": "done",
            "hook_event_name": "Stop",
        },
        transcript_path=transcript,
    )
    after = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

    assert exit_code == 0
    assert after - before == 2  # (1000 + 200*5) / 1000


def test_stop_hook_idempotent_for_same_turn(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys, tmp_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        _turn_transcript(
            "prompt-dup",
            "asst-1",
            text="final",
            input_tokens=5000,
            output_tokens=1000,
        ),
    )
    payload = {
        "session_id": "sess-abc",
        "prompt_id": "prompt-dup",
        "last_assistant_message": "final",
        "hook_event_name": "Stop",
    }

    _run_stop_hook(monkeypatch, config_path, db_path, capsys, stdin_payload=payload, transcript_path=transcript)
    used_after_first = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

    _run_stop_hook(monkeypatch, config_path, db_path, capsys, stdin_payload=payload, transcript_path=transcript)
    used_after_second = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

    assert used_after_second == used_after_first


def test_stop_hook_fail_open_on_missing_usage(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys, tmp_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [{"type": "user", "message": {"content": "no assistant yet"}}])

    before = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units
    exit_code, _, _ = _run_stop_hook(
        monkeypatch,
        config_path,
        db_path,
        capsys,
        stdin_payload={
            "session_id": "sess-abc",
            "prompt_id": "prompt-missing",
            "hook_event_name": "Stop",
        },
        transcript_path=transcript,
    )
    after = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

    assert exit_code == 0
    assert after == before


def test_stop_hook_fail_open_on_malformed_transcript(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys, tmp_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("not json\n{broken\n", encoding="utf-8")

    before = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units
    exit_code, _, _ = _run_stop_hook(
        monkeypatch,
        config_path,
        db_path,
        capsys,
        stdin_payload={
            "session_id": "sess-abc",
            "prompt_id": "prompt-1",
            "hook_event_name": "Stop",
        },
        transcript_path=transcript,
    )
    after = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

    assert exit_code == 0
    assert after == before


def test_stop_hook_not_configured_is_no_op(monkeypatch, config_path: Path, db_path: Path, capsys, tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        _turn_transcript("prompt-1", "asst-1", text="hello", input_tokens=1000, output_tokens=100),
    )

    exit_code, out, err = _run_stop_hook(
        monkeypatch,
        config_path,
        db_path,
        capsys,
        stdin_payload={"session_id": "sess-abc", "hook_event_name": "Stop"},
        transcript_path=transcript,
    )
    assert exit_code == 0
    assert out == ""
    assert err == ""


def test_stop_hook_does_not_consume_stale_entry_during_transcript_race(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys, tmp_path: Path
) -> None:
    """Simulate Stop firing while only the prior turn is complete on disk.

    The transcript ends with a valid OLD assistant-usage line plus a partial
    in-flight JSON line for the current turn. The hook must not charge the
    stale tokens; it should wait until the real current-turn entry lands.
    """
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    transcript = tmp_path / "session.jsonl"
    stale_entries = _turn_transcript(
        "prompt-old",
        "asst-old",
        text="old response",
        input_tokens=9000,
        output_tokens=9000,
    )
    _write_transcript(transcript, stale_entries)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant","uuid":"asst-new","parentUuid":"user-new","message":{"content":[{"type":"text","text":"new response"}],"usage":{"input_tokens":1000,"output_tokens":100')

    poll_calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        poll_calls["n"] += 1
        if poll_calls["n"] == 1:
            _write_transcript(
                transcript,
                [
                    *stale_entries,
                    *_turn_transcript(
                        "prompt-new",
                        "asst-new",
                        user_uuid="user-new",
                        parent_uuid="asst-old",
                        text="new response",
                        input_tokens=1000,
                        output_tokens=100,
                    ),
                ],
            )

    monkeypatch.setattr(
        "claude_share.integrations.claude_code.stop_hook.wait_for_current_turn_usage",
        lambda path, verification, **kwargs: wait_for_current_turn_usage(
            path,
            verification,
            max_wait_seconds=1.0,
            initial_interval_seconds=0.01,
            sleep=fake_sleep,
        ),
    )

    before = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units
    exit_code, _, _ = _run_stop_hook(
        monkeypatch,
        config_path,
        db_path,
        capsys,
        stdin_payload={
            "session_id": "sess-abc",
            "prompt_id": "prompt-new",
            "last_assistant_message": "new response",
            "hook_event_name": "Stop",
        },
        transcript_path=transcript,
    )
    after = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

    assert exit_code == 0
    assert poll_calls["n"] >= 1
    assert after - before == 2  # current turn only: (1000 + 100*5) / 1000
    assert after - before != tokens_to_units(9000, 9000)


def test_stop_hook_fail_open_when_stale_entry_never_replaced(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys, tmp_path: Path
) -> None:
    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        _turn_transcript(
            "prompt-old",
            "asst-old",
            text="old response",
            input_tokens=9000,
            output_tokens=9000,
        ),
    )

    monkeypatch.setattr(
        "claude_share.integrations.claude_code.stop_hook.wait_for_current_turn_usage",
        lambda path, verification, **kwargs: wait_for_current_turn_usage(
            path,
            verification,
            max_wait_seconds=0.05,
            initial_interval_seconds=0.01,
            sleep=lambda _s: None,
        ),
    )

    before = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units
    exit_code, _, _ = _run_stop_hook(
        monkeypatch,
        config_path,
        db_path,
        capsys,
        stdin_payload={
            "session_id": "sess-abc",
            "prompt_id": "prompt-new",
            "last_assistant_message": "new response",
            "hook_event_name": "Stop",
        },
        transcript_path=transcript,
    )
    after = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

    assert exit_code == 0
    assert after == before


def test_stop_hook_consumes_via_remote_server(
    monkeypatch,
    service: QuotaService,
    config_path: Path,
    tmp_path: Path,
    capsys,
) -> None:
    import socket
    import threading
    import time

    import uvicorn
    from fastapi.testclient import TestClient

    pool = service.create_pool("Pool", ["Alice"])
    alice = service.list_members(pool.id)[0]

    app = create_app(uow_factory=service._uow_factory)
    test_client = TestClient(app)
    device_resp = test_client.post("/devices", json={"user_id": alice.user_id, "device_name": "remote"}).json()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=0.5)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        raise RuntimeError("test server did not start in time")

    save_local_identity(
        config_path,
        _make_identity(
            pool.id,
            alice.id,
            alice.user_id,
            server_url=base_url,
            device_token=device_resp["token"],
        ),
    )

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        _turn_transcript(
            "prompt-remote",
            "asst-remote",
            text="remote done",
            input_tokens=2000,
            output_tokens=400,
        ),
    )

    try:
        before = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units
        exit_code, _, _ = _run_stop_hook(
            monkeypatch,
            config_path,
            tmp_path / "unused.db",
            capsys,
            stdin_payload={
                "session_id": "sess-remote",
                "prompt_id": "prompt-remote",
                "last_assistant_message": "remote done",
                "hook_event_name": "Stop",
            },
            transcript_path=transcript,
        )
        after = service.get_status(alice.id).windows[WindowType.FIVE_HOUR].used_units

        assert exit_code == 0
        assert after - before == 4  # (2000 + 400*5) / 1000
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_user_prompt_submit_blocks_only_when_no_remaining_capacity(
    monkeypatch, service: QuotaService, config_path: Path, db_path: Path, capsys
) -> None:
    pool = service.create_pool("Pool", ["Alice", "Bob"])
    alice = service.list_members(pool.id)[0]
    save_local_identity(config_path, _make_identity(pool.id, alice.id, alice.user_id))

    service.consume(alice.id, WindowType.FIVE_HOUR, 4999, "almost-full")

    monkeypatch.setenv("CLAUDE_SHARE_CONFIG", str(config_path))
    monkeypatch.setenv("CLAUDE_SHARE_DB", str(db_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    exit_code = hook.main()
    assert exit_code == 0

    service.consume(alice.id, WindowType.FIVE_HOUR, 1, "last-unit")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    exit_code = hook.main()
    assert exit_code == 2


def test_wait_for_current_turn_usage_polls_until_verified_entry(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            _write_transcript(
                transcript,
                _turn_transcript(
                    "prompt-1",
                    "asst-1",
                    text="hello",
                    input_tokens=10,
                    output_tokens=5,
                ),
            )

    usage = wait_for_current_turn_usage(
        transcript,
        TurnVerification(prompt_id="prompt-1", last_assistant_message="hello"),
        max_wait_seconds=1.0,
        initial_interval_seconds=0.01,
        sleep=fake_sleep,
    )
    assert usage is not None
    assert usage.input_tokens == 10
    assert calls["n"] == 1


def test_wait_for_last_assistant_usage_polls_until_available(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            _write_transcript(
                transcript,
                _turn_transcript(
                    "prompt-1",
                    "asst-1",
                    text="hello",
                    input_tokens=10,
                    output_tokens=5,
                ),
            )

    usage = wait_for_last_assistant_usage(
        transcript,
        max_wait_seconds=1.0,
        initial_interval_seconds=0.01,
        sleep=fake_sleep,
    )
    assert usage is not None
    assert usage.input_tokens == 10
    assert calls["n"] == 1
