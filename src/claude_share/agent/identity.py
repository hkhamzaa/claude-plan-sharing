"""Local identity: "which pool and which member_id is this machine
currently acting as", persisted as a small JSON file.

This is deliberately not a domain entity with its own SQLite table: it
describes *this machine's* session state, not shared pool/quota data, so
it doesn't belong in the shared database at all (a second device for the
same user_id has its own, independent local identity file - and, since a
user_id can join more than one pool, its own choice of which pool/member
it's currently pointed at). A flat JSON file at a fixed, well-known path
is the simplest thing that satisfies "persist this somewhere between CLI
invocations" - see docs/architecture.md for the full rationale and the
exact file format.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

#: Default location, mirroring the SQLite DB's default under the same
#: directory (see cli/main.py: DEFAULT_DB_PATH).
DEFAULT_CONFIG_PATH = Path.home() / ".claude-share" / "config.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalIdentity:
    """This machine's currently-configured identity.

    `pool_id`/`member_id` are None after `login()` but before `join_pool()`
    - a device can be logged in as a user without yet having chosen which
    pool/member it acts as.

    `server_url`/`device_token` (Milestone 5) are None for a purely local
    identity (Milestones 1-4's behavior, unchanged) and both set together
    when this machine has instead registered against a central server via
    `agent.commands.login_remote()` - see `agent/remote_client.py`. Exactly
    one of "local SQLite" or "remote HTTP" mode is active for a given
    identity at a time; `cli/main.py` decides which by checking whether
    `server_url` is set, never by inspecting both.
    """

    pool_id: str | None
    member_id: str | None
    user_id: str
    device_id: str
    device_name: str
    server_url: str | None = None
    device_token: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.server_url is not None


def load_local_identity(config_path: str | Path) -> LocalIdentity | None:
    """Return the locally-configured identity, or None if not logged in yet."""
    path = Path(config_path)
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    return LocalIdentity(
        pool_id=data.get("pool_id"),
        member_id=data.get("member_id"),
        user_id=data["user_id"],
        device_id=data["device_id"],
        device_name=data["device_name"],
        server_url=data.get("server_url"),
        device_token=data.get("device_token"),
    )


def save_local_identity(config_path: str | Path, identity: LocalIdentity) -> None:
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(identity), indent=2) + "\n", encoding="utf-8")
