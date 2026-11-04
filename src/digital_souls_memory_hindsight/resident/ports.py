"""Snapshot reading and committed resident provision storage boundaries."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..character_profile.domain import CharacterProfile
from ..evidence.ports import EvidenceStore
from ..identity.ports import IdentitySnapshot
from ..recall.ports import ProvisionSpan
from ..voice.domain import VoiceExemplar


class ResidentSourcesChanged(Exception):
    """A selected source was deleted or accepted for deletion before commit."""


@dataclass(frozen=True)
class ResidentProvisionRecord:
    provision_id: str
    character_id: str
    session_id: str
    surface: str
    query: str | None
    sensitivity: str | None
    classifier_version: str | None
    core_identity_version: int | None
    temperament_version: int | None
    personality_version: int | None
    exemplar_ids: tuple[str, ...]
    profile_revision: int | None
    profile_entries: tuple[ProvisionSpan, ...]
    processing_time_ms: int
    created_at: datetime


class ResidentRead(Protocol):
    @property
    def evidence(self) -> EvidenceStore: ...

    def identity(self) -> IdentitySnapshot: ...
    def exemplars(self) -> tuple[VoiceExemplar, ...]: ...
    def profile(self) -> CharacterProfile | None: ...
    def turns_under_deletion(self, turn_ids: frozenset[str]) -> frozenset[str]: ...


class ResidentStore(Protocol):
    def snapshot(self, character_id: str) -> AbstractContextManager[ResidentRead]: ...
    def add(self, record: ResidentProvisionRecord, source_turn_ids: frozenset[str]) -> None: ...
