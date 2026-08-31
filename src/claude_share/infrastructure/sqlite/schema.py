"""SQLite schema definition and initialisation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    member_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES pools(id),
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_members_user_id
    ON members(user_id);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    device_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_devices_user_id
    ON devices(user_id);

CREATE TABLE IF NOT EXISTS allocations (
    member_id TEXT PRIMARY KEY REFERENCES members(id),
    bps INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_windows (
    member_id TEXT NOT NULL REFERENCES members(id),
    window_type TEXT NOT NULL,
    window_start TEXT NOT NULL,
    reset_at TEXT NOT NULL,
    allocation_units INTEGER NOT NULL,
    usage_units INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (member_id, window_type)
);

CREATE TABLE IF NOT EXISTS usage_records (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(id),
    window_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_records_member
    ON usage_records(member_id, window_type);

CREATE TABLE IF NOT EXISTS capacity_requests (
    id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES pools(id),
    requester_member_id TEXT NOT NULL REFERENCES members(id),
    target_member_id TEXT NOT NULL REFERENCES members(id),
    window_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    expires_at TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS capacity_grants (
    id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES pools(id),
    source_member_id TEXT NOT NULL REFERENCES members(id),
    recipient_member_id TEXT NOT NULL REFERENCES members(id),
    window_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_capacity_grants_source
    ON capacity_grants(source_member_id, window_type, status);
CREATE INDEX IF NOT EXISTS idx_capacity_grants_recipient
    ON capacity_grants(recipient_member_id, window_type, status);

CREATE TABLE IF NOT EXISTS shared_consumption_records (
    id TEXT PRIMARY KEY,
    usage_record_id TEXT NOT NULL REFERENCES usage_records(id),
    grant_id TEXT NOT NULL REFERENCES capacity_grants(id),
    amount INTEGER NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shared_consumption_usage_record
    ON shared_consumption_records(usage_record_id);
CREATE INDEX IF NOT EXISTS idx_shared_consumption_grant
    ON shared_consumption_records(grant_id);
"""


def init_db(db_path: str | Path) -> None:
    """Create the schema if it does not already exist. Safe to call repeatedly."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
