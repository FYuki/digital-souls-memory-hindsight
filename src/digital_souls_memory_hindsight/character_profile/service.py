"""Save profile settings and recheck original interval availability on reads."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from ..evidence.ports import EvidenceNotFound
from ..evidence.service import EvidenceService
from ..voice.domain import Span
from ..voice.ports import DeletionState, SourceTurns
from .domain import CharacterProfile, ProfileValidationError, entry_visible, validate_entries
from .ports import CharacterProfileStore, ProfileNotFound


def _utc_now() -> datetime:
    return datetime.now(UTC)


def available_entries(
    character_id: str, entries: tuple[Span, ...], evidence: SourceTurns, deletion: DeletionState
) -> tuple[Span, ...]:
    blocked = deletion.turns_under_deletion(frozenset(s.turn_id for s in entries))
    return tuple(
        span
        for span in entries
        if span.turn_id not in blocked and _entry_available(character_id, span, evidence)
    )


def _entry_available(character_id: str, span: Span, evidence: SourceTurns) -> bool:
    try:
        turn = evidence.get_turn(span.turn_id)
        scope = evidence.get_delivery_scope(span.turn_id)
    except EvidenceNotFound:
        return False
    return entry_visible(turn, scope, character_id, span)


class CharacterProfileService:
    def __init__(
        self,
        store: CharacterProfileStore,
        evidence: EvidenceService,
        deletion: DeletionState,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._evidence = evidence
        self._deletion = deletion
        self._clock = clock

    def put(
        self, character_id: str, char_limit: int, entries: tuple[Span, ...]
    ) -> CharacterProfile:
        self._evidence.require_character(character_id)
        validate_entries(entries, char_limit)
        blocked = self._deletion.turns_under_deletion(frozenset(s.turn_id for s in entries))
        if any(
            s.turn_id in blocked or not _entry_available(character_id, s, self._evidence)
            for s in entries
        ):
            raise ProfileValidationError()
        return self._store.save(character_id, char_limit, entries, self._clock())

    def get(self, character_id: str) -> CharacterProfile:
        self._evidence.require_character(character_id)
        value = self._store.get(character_id)
        if value is None:
            raise ProfileNotFound()
        entries = available_entries(character_id, value.entries, self._evidence, self._deletion)
        return replace(value, entries=entries)
