"""Register approved exemplars and recheck their availability on every read."""

from collections.abc import Callable
from datetime import UTC, datetime

from ..consistency.ports import Reviewer, checked_review
from ..evidence.ports import EvidenceNotFound
from ..evidence.service import EvidenceService
from ..identity.ports import LLMGateway
from .domain import (
    TYPICAL_SITUATION,
    Span,
    VoiceExemplar,
    VoiceRegistration,
    VoiceReview,
    VoiceValidationError,
    check_answer,
    check_question,
    rank,
    span_visible,
    validate_shape,
)
from .ports import (
    DeletionState,
    IdentityReader,
    SourceTurns,
    VoiceNotFound,
    VoiceStore,
    VoiceUnavailable,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def exemplar_available(
    value: VoiceExemplar, evidence: SourceTurns, deletion: DeletionState
) -> bool:
    if value.source == "canonical_seed":
        return True
    spans = tuple(s for s in (value.answer_span, value.question_span) if s is not None)
    if deletion.turns_under_deletion(frozenset(s.turn_id for s in spans)):
        return False
    try:
        assert value.answer_span is not None
        answer_turn = evidence.get_turn(value.answer_span.turn_id)
        check_answer(answer_turn, value.character_id, value.answer_span, value.answer)
        if value.question_span is not None:
            assert value.question is not None
            question_turn = evidence.get_turn(value.question_span.turn_id)
            check_question(answer_turn, question_turn, value.question_span, value.question)
        for span in spans:
            turn = evidence.get_turn(span.turn_id)
            scope = evidence.get_delivery_scope(span.turn_id)
            if not span_visible(turn, scope, value.character_id, span):
                return False
    except (EvidenceNotFound, VoiceValidationError):
        return False
    return True


class VoiceService:
    def __init__(
        self,
        store: VoiceStore,
        evidence: EvidenceService,
        deletion: DeletionState,
        identity: IdentityReader,
        gateway: LLMGateway | None,
        reviewer: Reviewer | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._evidence = evidence
        self._deletion = deletion
        self._identity = identity
        self._gateway = gateway
        self._reviewer = reviewer
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
    ) -> VoiceRegistration:
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
        if not exemplar_available(value, self._evidence, self._deletion):
            raise VoiceValidationError()
        review = None if self._store.reviewed(character_id) else self._initial_review(value)
        committed_review = self._store.add(value, review)
        return VoiceRegistration(value, review if committed_review else None)

    def _initial_review(self, value: VoiceExemplar) -> VoiceReview:
        if self._gateway is None or self._reviewer is None:
            raise VoiceUnavailable()
        try:
            current = self._identity.current(value.character_id)
            temperament = None if current.temperament is None else current.temperament.description
            response = self._gateway.respond_in_voice(
                TYPICAL_SITUATION, value.answer, value.question, temperament
            )
            if not isinstance(response, str) or not response.strip():
                raise VoiceUnavailable()
            result = checked_review(
                self._reviewer.review_voice(
                    TYPICAL_SITUATION, response, value.answer, value.question, temperament
                )
            )
        except Exception as error:
            raise VoiceUnavailable() from error
        return VoiceReview(result, self._clock())

    def get(self, character_id: str, exemplar_id: str) -> VoiceExemplar:
        self._evidence.require_character(character_id)
        value = self._store.get(character_id, exemplar_id)
        if value is None or not exemplar_available(value, self._evidence, self._deletion):
            raise VoiceNotFound()
        return value

    def list(self, character_id: str, query: str | None = None) -> tuple[VoiceExemplar, ...]:
        self._evidence.require_character(character_id)
        values = tuple(
            v
            for v in self._store.list(character_id)
            if exemplar_available(v, self._evidence, self._deletion)
        )
        return rank(values, query)
