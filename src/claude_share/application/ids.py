"""Shared id generation for entities created by the application layer."""

from __future__ import annotations

import uuid


def new_id() -> str:
    return str(uuid.uuid4())
