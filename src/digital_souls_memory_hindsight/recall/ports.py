"""Memory search boundary, independent of the SDK and HTTP."""

from dataclasses import dataclass
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
