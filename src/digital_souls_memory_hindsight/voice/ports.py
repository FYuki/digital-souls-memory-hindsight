"""Persistence and deletion-state boundaries for voice exemplars."""

from typing import Protocol

from ..evidence.delivery import DeliveryScope
from ..evidence.domain import Turn
from ..identity.ports import IdentitySnapshot
from .domain import VoiceExemplar, VoiceReview


class VoiceConflict(Exception):
    """The character already has this exemplar identifier."""


class VoiceNotFound(Exception):
    """The exemplar is absent or no longer available."""


class VoiceUnavailable(Exception):
    """The initial voice consistency review could not be completed."""


class IdentityReader(Protocol):
    def current(self, character_id: str) -> IdentitySnapshot: ...


class VoiceStore(Protocol):
    def reviewed(self, character_id: str) -> bool: ...

    def add(self, value: VoiceExemplar, review: VoiceReview | None) -> bool: ...

    def get(self, character_id: str, exemplar_id: str) -> VoiceExemplar | None: ...

    def list(self, character_id: str) -> tuple[VoiceExemplar, ...]: ...


class DeletionState(Protocol):
    def turns_under_deletion(self, turn_ids: frozenset[str]) -> frozenset[str]: ...


class SourceTurns(Protocol):
    def get_turn(self, turn_id: str) -> Turn: ...

    def get_delivery_scope(self, turn_id: str) -> DeliveryScope | None: ...
