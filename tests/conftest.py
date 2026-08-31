from __future__ import annotations

from pathlib import Path

import pytest

from claude_share.application.agent_service import AgentService
from claude_share.application.capacity_service import CapacityService
from claude_share.application.quota_service import QuotaService
from claude_share.infrastructure.sqlite.schema import init_db
from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "claude_share_test.db"
    init_db(path)
    return path


@pytest.fixture
def uow_factory(db_path: Path):
    return lambda: SqliteUnitOfWork(db_path)


@pytest.fixture
def service(db_path: Path) -> QuotaService:
    return QuotaService(uow_factory=lambda: SqliteUnitOfWork(db_path))


@pytest.fixture
def capacity_service(db_path: Path) -> CapacityService:
    return CapacityService(uow_factory=lambda: SqliteUnitOfWork(db_path))


@pytest.fixture
def agent_service(db_path: Path) -> AgentService:
    return AgentService(uow_factory=lambda: SqliteUnitOfWork(db_path))


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Path for a local identity config file - not created until saved to."""
    return tmp_path / "config.json"
