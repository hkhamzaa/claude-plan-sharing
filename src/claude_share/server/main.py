"""Entrypoint for the `claude-share-server` console script (Milestone 5).

Reads configuration from environment variables (documented in README.md,
"Running the central server") and starts the FastAPI app from `app.py`
under uvicorn. This is the only module in this milestone that assumes a
real Postgres deployment target - everything above it (`app.py`,
`routes.py`) is backend-agnostic via `uow_factory`, and everything below it
(`infrastructure/postgres/`) is transport-agnostic.

Does **not** terminate TLS. Run this behind a reverse proxy (nginx, Caddy,
an ALB, ...) for any real deployment - see docs/architecture.md and
README.md, "Transport security", for why that isn't optional: bearer
tokens and quota data would otherwise cross the network in cleartext.
"""

from __future__ import annotations

import os

import uvicorn

from claude_share.infrastructure.postgres.schema import init_db
from claude_share.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from claude_share.server.app import create_app

DEFAULT_DSN = "postgresql://localhost:5432/claude_share"


def run() -> None:
    dsn = os.environ.get("CLAUDE_SHARE_DATABASE_URL", DEFAULT_DSN)
    host = os.environ.get("CLAUDE_SHARE_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("CLAUDE_SHARE_SERVER_PORT", "8000"))

    init_db(dsn)
    app = create_app(uow_factory=lambda: PostgresUnitOfWork(dsn))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
