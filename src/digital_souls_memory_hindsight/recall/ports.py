"""Memory search boundary, independent of the SDK and HTTP."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class RecallSourceNotFound(Exception):
    """The requested recall source is absent or outside perception."""


class RecallUnavailable(Exception):
    """Recall dependencies are unavailable."""


@dataclass(frozen=True)
class RecalledFact:
    id: str
    kind: str
    text: str
    document_id: str
    chunk_text: str


class MemoryRecall(Protocol):
    async def recall(self, bank_id: str, query: str) -> tuple[RecalledFact, ...]: ...


class ProvisionConflict(Exception):
    """The provision identifier has already been recorded."""


@dataclass(frozen=True)
class ProvisionSpan:
    turn_id: str
    start: int
    end: int


@dataclass(frozen=True)
class ProvisionRecord:
    provision_id: str
    kind: str
    character_id: str
    session_id: str
    surface: str
    query: str | None
    turn_id: str | None
    sensitivity: str | None
    classifier_version: str | None
    memory_ids: tuple[str, ...]
    source_spans: tuple[ProvisionSpan, ...]
    verbatim_anchor_ids: tuple[str, ...]
    temperament_version: str | None
    personality_version: str | None
    created_at: datetime


class ProvisionStore(Protocol):
    def add(self, value: ProvisionRecord) -> None: ...
