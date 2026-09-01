"""Milestone 5 HTTP-layer tests: routing, request/response shapes, domain
error -> status code mapping, and auth - run against a SQLite-backed app
(fast, no external dependency) since none of this cares which UnitOfWork
backs it (see server/app.py's docstring: `uow_factory` is injected).
Postgres-specific correctness (locking/concurrency) is proven separately in
tests/test_postgres_unit_of_work.py; the full stack together (HTTP -> auth
-> service -> PostgresUnitOfWork -> real Postgres) is proven end-to-end in
tests/test_server_e2e_postgres.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import DeadlockDetected

from claude_share.infrastructure.sqlite.schema import init_db
from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from claude_share.server.app import create_app
from claude_share.server.errors import DEADLOCK_DETECTED_DETAIL


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "server_test.db"
    init_db(db_path)
    app = create_app(uow_factory=lambda: SqliteUnitOfWork(db_path))
    return TestClient(app)


def _create_pool(client: TestClient, name: str, member_names: list[str]) -> dict:
    r = client.post("/pools", json={"name": name, "member_names": member_names})
    assert r.status_code == 201, r.text
    return r.json()


def _register_device(client: TestClient, user_id: str, device_name: str = "Test Device") -> tuple[str, dict]:
    """Returns (bearer_token, device_json)."""
    r = client.post("/devices", json={"user_id": user_id, "device_name": device_name})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["token"], body["device"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pool_and_tokens(client: TestClient) -> dict:
    """A pool of Alice/Bob, each with a registered device+token."""
    created = _create_pool(client, "Pool", ["Alice", "Bob"])
    alice, bob = created["members"]
    alice_token, _ = _register_device(client, alice["user_id"], "Alice's Laptop")
    bob_token, _ = _register_device(client, bob["user_id"], "Bob's Laptop")
    return {
        "pool_id": created["pool"]["id"],
        "alice": alice,
        "bob": bob,
        "alice_token": alice_token,
        "bob_token": bob_token,
    }


# --- pool creation / device registration (bootstrap, no auth) ---------------


def test_create_pool_requires_no_auth(client: TestClient) -> None:
    r = client.post("/pools", json={"name": "P", "member_names": ["Solo"]})
    assert r.status_code == 201
    body = r.json()
    assert body["pool"]["member_count"] == 1
    assert body["members"][0]["display_name"] == "Solo"


def test_register_device_requires_no_auth_and_returns_token(client: TestClient) -> None:
    created = _create_pool(client, "P", ["Solo"])
    user_id = created["members"][0]["user_id"]
    r = client.post("/devices", json={"user_id": user_id, "device_name": "Laptop"})
    assert r.status_code == 201
    body = r.json()
    assert body["token"]
    assert body["device"]["user_id"] == user_id
    assert "token_hash" not in body["device"]  # never leaked over the wire


def test_register_device_for_unknown_user_id_is_404(client: TestClient) -> None:
    r = client.post("/devices", json={"user_id": "no-such-user", "device_name": "Laptop"})
    assert r.status_code == 404


# --- auth: missing / invalid / cross-member token ----------------------------


def test_request_with_no_token_is_rejected(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    r = client.get(f"/members/{alice['id']}/status")
    assert r.status_code == 401


def test_request_with_unknown_token_is_rejected(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    r = client.get(f"/members/{alice['id']}/status", headers=_auth("not-a-real-token"))
    assert r.status_code == 401


def test_malformed_authorization_header_is_rejected(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    r = client.get(f"/members/{alice['id']}/status", headers={"Authorization": "Basic somebase64"})
    assert r.status_code == 401


def test_valid_token_for_one_member_cannot_act_as_another(client: TestClient, pool_and_tokens: dict) -> None:
    bob = pool_and_tokens["bob"]
    alice_token = pool_and_tokens["alice_token"]

    r = client.get(f"/members/{bob['id']}/status", headers=_auth(alice_token))
    assert r.status_code == 403

    r = client.post(
        "/quota/consume",
        json={"member_id": bob["id"], "window_type": "five_hour", "amount": 100, "idempotency_key": "k1"},
        headers=_auth(alice_token),
    )
    assert r.status_code == 403


def test_valid_token_for_own_member_succeeds(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    r = client.get(f"/members/{alice['id']}/status", headers=_auth(pool_and_tokens["alice_token"]))
    assert r.status_code == 200
    assert r.json()["member_id"] == alice["id"]


# --- quota endpoints --------------------------------------------------------


def test_list_members_requires_auth(client: TestClient, pool_and_tokens: dict) -> None:
    r = client.get(f"/pools/{pool_and_tokens['pool_id']}/members")
    assert r.status_code == 401

    r = client.get(f"/pools/{pool_and_tokens['pool_id']}/members", headers=_auth(pool_and_tokens["bob_token"]))
    assert r.status_code == 200
    assert {m["display_name"] for m in r.json()} == {"Alice", "Bob"}


def test_check_quota_happy_path(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    r = client.post(
        "/quota/check",
        json={"member_id": alice["id"], "window_type": "five_hour", "amount": 100},
        headers=_auth(pool_and_tokens["alice_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is True
    assert body["remaining_units"] == 5000


def test_consume_then_second_read_reflects_persisted_state(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    headers = _auth(pool_and_tokens["alice_token"])

    r = client.post(
        "/quota/consume",
        json={"member_id": alice["id"], "window_type": "five_hour", "amount": 250, "idempotency_key": "consume-1"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is True

    r = client.get(f"/members/{alice['id']}/status", headers=headers)
    assert r.json()["windows"]["five_hour"]["used_units"] == 250


def test_consume_idempotent_replay_over_http(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    headers = _auth(pool_and_tokens["alice_token"])
    payload = {"member_id": alice["id"], "window_type": "five_hour", "amount": 250, "idempotency_key": "replay-1"}

    first = client.post("/quota/consume", json=payload, headers=headers)
    second = client.post("/quota/consume", json=payload, headers=headers)
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True

    status = client.get(f"/members/{alice['id']}/status", headers=headers).json()
    assert status["windows"]["five_hour"]["used_units"] == 250  # consumed once, not twice


def test_consume_insufficient_quota_returns_200_with_rejected_body(client: TestClient, pool_and_tokens: dict) -> None:
    """consume() rejecting for business reasons ("insufficient guaranteed +
    shared capacity") is a normal outcome, not an HTTP error - it returns
    accepted=False in a 200, exactly like the CLI's exit-code-2 (not a
    crash) for the same case."""
    alice = pool_and_tokens["alice"]
    headers = _auth(pool_and_tokens["alice_token"])
    r = client.post(
        "/quota/consume",
        json={"member_id": alice["id"], "window_type": "five_hour", "amount": 999999, "idempotency_key": "too-much"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is False
    assert r.json()["reason"] == "insufficient_quota"


def test_get_status_for_unknown_member_is_404(client: TestClient, pool_and_tokens: dict) -> None:
    r = client.get("/members/does-not-exist/status", headers=_auth(pool_and_tokens["alice_token"]))
    assert r.status_code == 404


def test_postgres_deadlock_detected_maps_to_409(client: TestClient, pool_and_tokens: dict) -> None:
    """Postgres advisory-lock deadlocks (SQLSTATE 40P01) must not surface as
    an unhandled 500 - they're transient contention, safely rolled back."""
    alice = pool_and_tokens["alice"]
    quota_service = client.app.state.quota_service
    with patch.object(
        quota_service,
        "consume",
        side_effect=DeadlockDetected("deadlock detected"),
    ):
        r = client.post(
            "/quota/consume",
            json={
                "member_id": alice["id"],
                "window_type": "five_hour",
                "amount": 100,
                "idempotency_key": "deadlock-test",
            },
            headers=_auth(pool_and_tokens["alice_token"]),
        )
    assert r.status_code == 409
    assert r.json()["detail"] == DEADLOCK_DETECTED_DETAIL


# --- capacity delegation endpoints -------------------------------------------


def test_request_approve_capacity_flow_over_http(client: TestClient, pool_and_tokens: dict) -> None:
    pool_id = pool_and_tokens["pool_id"]
    alice, bob = pool_and_tokens["alice"], pool_and_tokens["bob"]
    alice_headers = _auth(pool_and_tokens["alice_token"])
    bob_headers = _auth(pool_and_tokens["bob_token"])

    r = client.post(
        "/capacity/requests",
        json={
            "pool_id": pool_id,
            "requester_member_id": bob["id"],
            "target_member_id": alice["id"],
            "window_type": "five_hour",
            "amount": 500,
            "type": "shared",
        },
        headers=bob_headers,
    )
    assert r.status_code == 201, r.text
    request_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    # Bob cannot approve his own request (only the target/owner can).
    r = client.post(
        f"/capacity/requests/{request_id}/approve",
        json={"approving_member_id": bob["id"]},
        headers=bob_headers,
    )
    assert r.status_code == 403

    r = client.post(
        f"/capacity/requests/{request_id}/approve",
        json={"approving_member_id": alice["id"]},
        headers=alice_headers,
    )
    assert r.status_code == 200, r.text
    grant = r.json()
    assert grant["status"] == "active"
    assert grant["amount"] == 500

    r = client.get(f"/members/{alice['id']}/capacity?window=five_hour", headers=alice_headers)
    assert r.status_code == 200
    assert r.json()["shared_offered"] == 500


def test_reject_request_over_http(client: TestClient, pool_and_tokens: dict) -> None:
    pool_id = pool_and_tokens["pool_id"]
    alice, bob = pool_and_tokens["alice"], pool_and_tokens["bob"]

    r = client.post(
        "/capacity/requests",
        json={
            "pool_id": pool_id,
            "requester_member_id": bob["id"],
            "target_member_id": alice["id"],
            "window_type": "five_hour",
            "amount": 100,
            "type": "solid",
        },
        headers=_auth(pool_and_tokens["bob_token"]),
    )
    request_id = r.json()["id"]

    r = client.post(
        f"/capacity/requests/{request_id}/reject",
        json={"rejecting_member_id": alice["id"]},
        headers=_auth(pool_and_tokens["alice_token"]),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


def test_revoke_grant_over_http(client: TestClient, pool_and_tokens: dict) -> None:
    pool_id = pool_and_tokens["pool_id"]
    alice, bob = pool_and_tokens["alice"], pool_and_tokens["bob"]
    alice_headers = _auth(pool_and_tokens["alice_token"])
    bob_headers = _auth(pool_and_tokens["bob_token"])

    r = client.post(
        "/capacity/requests",
        json={
            "pool_id": pool_id,
            "requester_member_id": bob["id"],
            "target_member_id": alice["id"],
            "window_type": "five_hour",
            "amount": 100,
            "type": "solid",
        },
        headers=bob_headers,
    )
    request_id = r.json()["id"]
    grant_id = client.post(
        f"/capacity/requests/{request_id}/approve",
        json={"approving_member_id": alice["id"]},
        headers=alice_headers,
    ).json()["id"]

    # Bob (the recipient) cannot revoke - only the source (Alice) can.
    r = client.post(f"/capacity/grants/{grant_id}/revoke", json={"revoking_member_id": bob["id"]}, headers=bob_headers)
    assert r.status_code == 403

    r = client.post(
        f"/capacity/grants/{grant_id}/revoke", json={"revoking_member_id": alice["id"]}, headers=alice_headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"


def test_approve_request_insufficient_source_capacity_is_409(client: TestClient, pool_and_tokens: dict) -> None:
    pool_id = pool_and_tokens["pool_id"]
    alice, bob = pool_and_tokens["alice"], pool_and_tokens["bob"]

    r = client.post(
        "/capacity/requests",
        json={
            "pool_id": pool_id,
            "requester_member_id": bob["id"],
            "target_member_id": alice["id"],
            "window_type": "five_hour",
            "amount": 999999,
            "type": "solid",
        },
        headers=_auth(pool_and_tokens["bob_token"]),
    )
    request_id = r.json()["id"]

    r = client.post(
        f"/capacity/requests/{request_id}/approve",
        json={"approving_member_id": alice["id"]},
        headers=_auth(pool_and_tokens["alice_token"]),
    )
    assert r.status_code == 409


def test_approve_already_approved_request_is_409(client: TestClient, pool_and_tokens: dict) -> None:
    pool_id = pool_and_tokens["pool_id"]
    alice, bob = pool_and_tokens["alice"], pool_and_tokens["bob"]
    alice_headers = _auth(pool_and_tokens["alice_token"])

    r = client.post(
        "/capacity/requests",
        json={
            "pool_id": pool_id,
            "requester_member_id": bob["id"],
            "target_member_id": alice["id"],
            "window_type": "five_hour",
            "amount": 100,
            "type": "solid",
        },
        headers=_auth(pool_and_tokens["bob_token"]),
    )
    request_id = r.json()["id"]
    client.post(f"/capacity/requests/{request_id}/approve", json={"approving_member_id": alice["id"]}, headers=alice_headers)

    r = client.post(f"/capacity/requests/{request_id}/approve", json={"approving_member_id": alice["id"]}, headers=alice_headers)
    assert r.status_code == 409


def test_approve_unknown_request_is_404(client: TestClient, pool_and_tokens: dict) -> None:
    r = client.post(
        "/capacity/requests/does-not-exist/approve",
        json={"approving_member_id": pool_and_tokens["alice"]["id"]},
        headers=_auth(pool_and_tokens["alice_token"]),
    )
    assert r.status_code == 404


# --- devices -----------------------------------------------------------------


def test_list_devices_for_self(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    r = client.get(f"/users/{alice['user_id']}/devices", headers=_auth(pool_and_tokens["alice_token"]))
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["device_name"] == "Alice's Laptop"


def test_list_devices_for_another_user_is_forbidden(client: TestClient, pool_and_tokens: dict) -> None:
    bob = pool_and_tokens["bob"]
    r = client.get(f"/users/{bob['user_id']}/devices", headers=_auth(pool_and_tokens["alice_token"]))
    assert r.status_code == 403


# --- Milestone 7 dashboard read endpoints ------------------------------------


def test_list_pending_requests_requires_auth(client: TestClient, pool_and_tokens: dict) -> None:
    alice = pool_and_tokens["alice"]
    r = client.get(f"/members/{alice['id']}/capacity/requests/pending")
    assert r.status_code == 401


def test_list_pending_requests_only_incoming_for_target(client: TestClient, pool_and_tokens: dict) -> None:
    pool_id = pool_and_tokens["pool_id"]
    alice, bob = pool_and_tokens["alice"], pool_and_tokens["bob"]
    alice_headers = _auth(pool_and_tokens["alice_token"])
    bob_headers = _auth(pool_and_tokens["bob_token"])

    r = client.post(
        "/capacity/requests",
        json={
            "pool_id": pool_id,
            "requester_member_id": bob["id"],
            "target_member_id": alice["id"],
            "window_type": "five_hour",
            "amount": 100,
            "type": "solid",
        },
        headers=bob_headers,
    )
    assert r.status_code == 201, r.text
    request_id = r.json()["id"]

    r = client.get(f"/members/{alice['id']}/capacity/requests/pending", headers=alice_headers)
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == request_id
    assert r.json()[0]["target_member_id"] == alice["id"]

    r = client.get(f"/members/{bob['id']}/capacity/requests/pending", headers=bob_headers)
    assert r.status_code == 200
    assert r.json() == []


def test_list_pending_requests_for_other_member_is_forbidden(client: TestClient, pool_and_tokens: dict) -> None:
    bob = pool_and_tokens["bob"]
    r = client.get(
        f"/members/{bob['id']}/capacity/requests/pending",
        headers=_auth(pool_and_tokens["alice_token"]),
    )
    assert r.status_code == 403


def test_list_active_grants_after_approval(client: TestClient, pool_and_tokens: dict) -> None:
    pool_id = pool_and_tokens["pool_id"]
    alice, bob = pool_and_tokens["alice"], pool_and_tokens["bob"]
    alice_headers = _auth(pool_and_tokens["alice_token"])
    bob_headers = _auth(pool_and_tokens["bob_token"])

    r = client.post(
        "/capacity/requests",
        json={
            "pool_id": pool_id,
            "requester_member_id": bob["id"],
            "target_member_id": alice["id"],
            "window_type": "five_hour",
            "amount": 150,
            "type": "shared",
        },
        headers=bob_headers,
    )
    request_id = r.json()["id"]
    client.post(
        f"/capacity/requests/{request_id}/approve",
        json={"approving_member_id": alice["id"]},
        headers=alice_headers,
    )

    r = client.get(f"/members/{alice['id']}/capacity/grants", headers=alice_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["sent"]) == 1
    assert body["sent"][0]["amount"] == 150

    r = client.get(f"/members/{bob['id']}/capacity/grants", headers=bob_headers)
    assert r.status_code == 200
    assert len(r.json()["received"]) == 1
