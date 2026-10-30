"""Anchor metadata and persistence boundary; original text stays in the ledger."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class VerbatimAnchor:
    character_id: str
    anchor_id: str
    turn_id: str
    kind: str
    reason: str
    approved_by: str
    registered_at: datetime


class AnchorConflict(Exception):
    """This character already has an anchor with that identifier."""


class AnchorNotFound(Exception):
    """The requested anchor is absent."""


class AnchorStore(Protocol):
    def add(self, value: VerbatimAnchor) -> None: ...

    def get(self, character_id: str, anchor_id: str) -> VerbatimAnchor | None: ...

    def list(self, character_id: str) -> tuple[VerbatimAnchor, ...]: ...

    def match(self, candidates: tuple[tuple[str, str], ...], query: str) -> frozenset[str]: ...
