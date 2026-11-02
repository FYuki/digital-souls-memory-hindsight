"""Persistence boundary for deletion requests and retained blocking scopes."""

from datetime import datetime, timedelta
from typing import Literal, Protocol

from .domain import DeletionItem, DeletionRequest, DeletionStatus
from .domain import DeletionTransitionRejected as DeletionTransitionRejected


class DeletionConflict(Exception):
    """The request identifier already belongs to another selection."""


class DeletionNotFound(Exception):
    """The deletion request does not exist."""


class DeletionTargetNotFound(Exception):
    """A selected turn does not exist."""


class DeletionStore(Protocol):
    def get(self, request_id: str) -> DeletionRequest | None: ...
    def add(
        self, request: DeletionRequest, blocks: frozenset[tuple[str, str]]
    ) -> DeletionRequest: ...
    def save_transition(
        self, previous_status: DeletionStatus, updated: DeletionRequest
    ) -> DeletionRequest: ...
    def blocked_sessions(self, character_id: str) -> frozenset[str]: ...


class DeletionQueue(Protocol):
    def claim(self, now: datetime, lease_for: timedelta) -> DeletionItem | None: ...
    def complete(self, item: DeletionItem, now: datetime) -> bool: ...
    def defer(
        self, item: DeletionItem, now: datetime, available_at: datetime, reason: str
    ) -> bool: ...
    def fail(self, item: DeletionItem, now: datetime, reason: str) -> bool: ...
    def renew(self, item: DeletionItem, now: datetime, lease_for: timedelta) -> bool: ...
    def save_progress(
        self, item: DeletionItem, progress: dict[str, object], now: datetime
    ) -> None: ...


class DeletionLedger(Protocol):
    def affected_pairs(self, request_id: str) -> tuple[tuple[str, str], ...]: ...
    def projections_busy(self, request_id: str, now: datetime) -> bool: ...
    def delete_turns(
        self, item: DeletionItem, request: DeletionRequest, now: datetime
    ) -> dict[str, object]: ...
    def rebuild_statuses(self, request_id: str) -> tuple[str, ...]: ...
    def delete_provisions_for_memories(
        self, item: DeletionItem, character: str, ids: frozenset[str], now: datetime
    ) -> dict[str, object]: ...


class DeletionPersistence(DeletionQueue, DeletionLedger, Protocol):
    """The queue and ledger transactions used by one deletion executor."""


class MemoryIndex(Protocol):
    async def bank_busy(self, bank_id: str) -> bool: ...
    async def memory_ids(self, bank_id: str) -> frozenset[str]: ...
    async def observation_sources(self, bank_id: str) -> dict[str, frozenset[str]]: ...
    async def clear_memory_observations(self, bank_id: str, memory_id: str) -> None: ...
    async def clear_observations(self, bank_id: str) -> None: ...


DeletionStepResult = Literal["completed", "waiting", "failed", "not_configured"]


class DeletionStep(Protocol):
    @property
    def queue(self) -> DeletionQueue: ...
    async def step(self, item: DeletionItem) -> DeletionStepResult: ...
