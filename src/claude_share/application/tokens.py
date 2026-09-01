"""Device API token generation/hashing (Milestone 5).

An opaque, high-entropy bearer token is the whole auth mechanism for the
central server - see docs/architecture.md ("Minimal device-token auth") for
why this is intentionally simpler than OAuth/JWT. Only the SHA-256 hash of
a token is ever persisted (`Device.token_hash`); the plaintext token exists
only in memory on the response path of `AgentService.register_device()` and
in whatever the device operator saves it to locally (the local identity
config file, in remote mode).

SHA-256 (not a slow password-hashing KDF like bcrypt/argon2) is deliberate:
the token itself is 256 bits of `secrets`-grade randomness, not a
human-chosen password, so it isn't subject to offline dictionary/brute-force
guessing the way a password hash needs to defend against - a fast,
collision-resistant digest is the standard, sufficient choice for hashing
high-entropy opaque tokens (the same approach GitHub/GitLab personal access
tokens use).
"""

from __future__ import annotations

import hashlib
import secrets

#: 256 bits of randomness, URL-safe encoded - plenty to make guessing
#: infeasible for a small trusted-group deployment.
_TOKEN_BYTES = 32


def generate_device_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
