"""Register approved exemplars and recheck their availability on every read."""

from collections.abc import Callable
from datetime import UTC, datetime

from ..evidence.ports import EvidenceNotFound
from ..evidence.service import EvidenceService
from .domain import (
    Span,
    VoiceExemplar,
    VoiceValidationError,
    check_answer,
    check_question,
    rank,
    span_visible,
    validate_shape,
)
from .ports import DeletionState, VoiceNotFound, VoiceStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


class VoiceService:
    def __init__(
        self,
        store: VoiceStore,
        evidence: EvidenceService,
        deletion: DeletionState,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._evidence = evidence
        self._deletion = deletion
        self._clock = clock

    def register(
        self,
        character_id: str,
        exemplar_id: str,
        source: str,
        answer: str,
        approved_by: str,
        question: str | None = None,
        answer_span: Span | None = None,
        question_span: Span | None = None,
    ) -> VoiceExemplar:
        self._evidence.require_character(character_id)
        value = VoiceExemplar(
            character_id,
            exemplar_id,
            source,
            answer,
            question,
            answer_span,
            question_span,
            approved_by,
            self._clock(),
        )
        validate_shape(value)
        if not self._available(value):
            raise VoiceValidationError()
        self._store.add(value)
        return value

    def _available(self, value: VoiceExemplar) -> bool:
        if value.source == "canonical_seed":
            return True
        spans = tuple(s for s in (value.answer_span, value.question_span) if s is not None)
        if self._deletion.turns_under_deletion(frozenset(s.turn_id for s in spans)):
            return False
        try:
            assert value.answer_span is not None
            answer_turn = self._evidence.get_turn(value.answer_span.turn_id)
            check_answer(answer_turn, value.character_id, value.answer_span, value.answer)
            if value.question_span is not None:
                assert value.question is not None
                question_turn = self._evidence.get_turn(value.question_span.turn_id)
                check_question(answer_turn, question_turn, value.question_span, value.question)
            for span in spans:
                turn = self._evidence.get_turn(span.turn_id)
                scope = self._evidence.get_delivery_scope(span.turn_id)
                if not span_visible(turn, scope, value.character_id, span):
                    return False
        except (EvidenceNotFound, VoiceValidationError):
            return False
        return True

    def get(self, character_id: str, exemplar_id: str) -> VoiceExemplar:
        self._evidence.require_character(character_id)
        value = self._store.get(character_id, exemplar_id)
        if value is None or not self._available(value):
            raise VoiceNotFound()
        return value

    def list(self, character_id: str, query: str | None = None) -> tuple[VoiceExemplar, ...]:
        self._evidence.require_character(character_id)
        values = tuple(v for v in self._store.list(character_id) if self._available(v))
        return rank(values, query)
