"""Repository and Unit-of-Work ports (abstract interfaces).

These are the seams between the domain/application layers and
infrastructure. Nothing here mentions SQLite, files, or connections -
concrete implementations live in `claude_share.infrastructure`. The
application layer only ever depends on these abstractions, which is what
makes the persistence backend swappable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from claude_share.domain.models import (
    Allocation,
    CapacityGrant,
    CapacityRequest,
    Device,
    Member,
    Pool,
    QuotaWindow,
    SharedConsumptionRecord,
    UsageRecord,
    WindowType,
)


class PoolRepository(ABC):
    @abstractmethod
    def add(self, pool: Pool) -> None: ...

    @abstractmethod
    def get(self, pool_id: str) -> Pool | None: ...


class MemberRepository(ABC):
    @abstractmethod
    def add(self, member: Member) -> None: ...

    @abstractmethod
    def get(self, member_id: str) -> Member | None: ...

    @abstractmethod
    def list_by_pool(self, pool_id: str) -> list[Member]: ...

    @abstractmethod
    def find_by_user_id(self, user_id: str) -> Member | None: ...


class DeviceRepository(ABC):
    @abstractmethod
    def add(self, device: Device) -> None: ...

    @abstractmethod
    def get(self, device_id: str) -> Device | None: ...

    @abstractmethod
    def list_by_user(self, user_id: str) -> list[Device]: ...

    @abstractmethod
    def find_by_token_hash(self, token_hash: str) -> Device | None:
        """Resolve a hashed bearer token back to the Device it belongs to
        (Milestone 5 server auth). Returns None for an unknown hash."""
        ...


class AllocationRepository(ABC):
    @abstractmethod
    def add(self, allocation: Allocation) -> None: ...

    @abstractmethod
    def get(self, member_id: str) -> Allocation | None: ...


class QuotaWindowRepository(ABC):
    @abstractmethod
    def add(self, window: QuotaWindow) -> None: ...

    @abstractmethod
    def get(self, member_id: str, window_type: WindowType) -> QuotaWindow | None: ...

    @abstractmethod
    def update(self, window: QuotaWindow) -> None: ...

    @abstractmethod
    def list_by_member(self, member_id: str) -> list[QuotaWindow]: ...


class UsageRecordRepository(ABC):
    @abstractmethod
    def add(self, record: UsageRecord) -> None: ...

    @abstractmethod
    def find_by_idempotency_key(self, idempotency_key: str) -> UsageRecord | None: ...


class CapacityRequestRepository(ABC):
    @abstractmethod
    def add(self, request: CapacityRequest) -> None: ...

    @abstractmethod
    def get(self, request_id: str) -> CapacityRequest | None: ...

    @abstractmethod
    def update(self, request: CapacityRequest) -> None: ...


class CapacityGrantRepository(ABC):
    @abstractmethod
    def add(self, grant: CapacityGrant) -> None: ...

    @abstractmethod
    def get(self, grant_id: str) -> CapacityGrant | None: ...

    @abstractmethod
    def update(self, grant: CapacityGrant) -> None: ...

    @abstractmethod
    def list_by_source(self, member_id: str, window_type: WindowType) -> list[CapacityGrant]: ...

    @abstractmethod
    def list_by_recipient(self, member_id: str, window_type: WindowType) -> list[CapacityGrant]: ...


class SharedConsumptionRecordRepository(ABC):
    @abstractmethod
    def add(self, record: SharedConsumptionRecord) -> None: ...

    @abstractmethod
    def list_by_usage_record(self, usage_record_id: str) -> list[SharedConsumptionRecord]: ...

    @abstractmethod
    def list_by_grant(self, grant_id: str) -> list[SharedConsumptionRecord]: ...


class UnitOfWork(ABC):
    """A single atomic transaction boundary exposing the repositories.

    Usage:

        with uow_factory() as uow:
            ...  # reads and/or writes via uow.pools / uow.members / etc.
            uow.commit()

    If the `with` block exits without an explicit commit() (including via
    an exception), the transaction is rolled back and no partial writes are
    visible - this is what gives consume() its atomicity guarantee.
    """

    pools: PoolRepository
    members: MemberRepository
    devices: DeviceRepository
    allocations: AllocationRepository
    windows: QuotaWindowRepository
    usage_records: UsageRecordRepository
    requests: CapacityRequestRepository
    grants: CapacityGrantRepository
    shared_consumption_records: SharedConsumptionRecordRepository

    @abstractmethod
    def __enter__(self) -> UnitOfWork: ...

    @abstractmethod
    def __exit__(self, exc_type, exc, tb) -> bool | None: ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...
