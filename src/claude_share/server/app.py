"""FastAPI application factory for the Milestone 5 central server.

`create_app(uow_factory)` is the one place that wires a concrete
`UnitOfWork` factory into `QuotaService`/`CapacityService`/`AgentService`
and exposes them over HTTP - the server-side composition root, playing the
same role `cli/main.py:_build_services()` plays for the local CLI. Taking
`uow_factory` as a parameter (rather than hard-coding a Postgres DSN here)
is what makes the whole HTTP layer testable against a throwaway SQLite or
Postgres database without starting a real server process - see
`tests/test_server_routes.py`. `server/main.py` is the thin entrypoint that
supplies the *real* factory (`PostgresUnitOfWork` against `DATABASE_URL`)
for actually running this as a deployed service.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.errors import DeadlockDetected

from claude_share.application.agent_service import AgentService
from claude_share.application.capacity_service import CapacityService
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import DomainError
from claude_share.domain.repository import UnitOfWork
from claude_share.server.errors import DEADLOCK_DETECTED_DETAIL, status_code_for
from claude_share.server.routes import router


def create_app(uow_factory: Callable[[], UnitOfWork]) -> FastAPI:
    app = FastAPI(
        title="Claude Share Central Server",
        description=(
            "Milestone 5: HTTP adapter over the same QuotaService/CapacityService/"
            "AgentService application logic the local CLI uses - see docs/architecture.md. "
            "Run behind TLS; see README.md for deployment notes."
        ),
    )
    app.state.uow_factory = uow_factory
    app.state.quota_service = QuotaService(uow_factory)
    app.state.capacity_service = CapacityService(uow_factory)
    app.state.agent_service = AgentService(uow_factory)

    app.include_router(router)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(status_code=status_code_for(exc), content={"detail": str(exc)})

    @app.exception_handler(DeadlockDetected)
    async def _deadlock_detected_handler(request, exc: DeadlockDetected) -> JSONResponse:  # noqa: ANN001
        _ = exc
        return JSONResponse(status_code=409, content={"detail": DEADLOCK_DETECTED_DETAIL})

    @app.exception_handler(ValueError)
    async def _value_error_handler(request, exc: ValueError) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    dashboard_dist = Path(__file__).resolve().parents[3] / "dashboard" / "dist"
    if dashboard_dist.is_dir():
        app.mount(
            "/dashboard",
            StaticFiles(directory=dashboard_dist, html=True),
            name="dashboard",
        )

    return app
