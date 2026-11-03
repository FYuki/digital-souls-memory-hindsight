"""Voice values, source constraints and deterministic ranking."""

import re
from dataclasses import dataclass
from datetime import datetime

from ..evidence.delivery import DeliveryScope
from ..evidence.domain import Turn


class VoiceValidationError(Exception):
    """An exemplar does not satisfy its source or approval constraints."""


@dataclass(frozen=True)
class Span:
    turn_id: str
    start: int
    end: int


@dataclass(frozen=True)
class VoiceExemplar:
    character_id: str
    exemplar_id: str
    source: str
    answer: str
    question: str | None
    answer_span: Span | None
    question_span: Span | None
    approved_by: str
    registered_at: datetime


def validate_shape(value: VoiceExemplar) -> None:
    if not value.answer or value.question == "" or not re.search(r"\S", value.approved_by):
        raise VoiceValidationError()
    if value.source == "canonical_seed":
        if value.answer_span is not None or value.question_span is not None:
            raise VoiceValidationError()
    elif value.source == "turn":
        if value.answer_span is None or (value.question is None) != (value.question_span is None):
            raise VoiceValidationError()
    else:
        raise VoiceValidationError()
    for span in (value.answer_span, value.question_span):
        if span is not None and not (0 <= span.start < span.end):
            raise VoiceValidationError()


def check_answer(turn: Turn, character_id: str, span: Span, text: str) -> None:
    if (
        turn.turn_id != span.turn_id
        or turn.kind != "utterance"
        or turn.actor != character_id
        or not (0 <= span.start < span.end <= len(turn.text))
        or turn.text[span.start : span.end] != text
    ):
        raise VoiceValidationError()


def check_question(answer_turn: Turn, turn: Turn, span: Span, text: str) -> None:
    if (
        turn.turn_id != span.turn_id
        or answer_turn.reply_to_turn_id != span.turn_id
        or not (0 <= span.start < span.end <= len(turn.text))
        or turn.text[span.start : span.end] != text
    ):
        raise VoiceValidationError()


def span_visible(turn: Turn, scope: DeliveryScope | None, character_id: str, span: Span) -> bool:
    return (
        character_id in turn.perceived_by
        and scope is not None
        and scope.spoken_until is not None
        and 0 <= span.start < span.end <= scope.spoken_until
    )


def score(query: str, text: str) -> int:
    size = 1 if len(query) == 1 else 2
    query_grams = {query[i : i + size] for i in range(len(query) - size + 1)}
    text_grams = {text[i : i + size] for i in range(len(text) - size + 1)}
    return len(query_grams & text_grams)


def rank(items: tuple[VoiceExemplar, ...], query: str | None) -> tuple[VoiceExemplar, ...]:
    def key(value: VoiceExemplar) -> tuple[int, str]:
        points = 0
        if query is not None:
            points = score(query, value.answer)
            if value.question is not None:
                points = max(points, score(query, value.question))
        return -points, value.exemplar_id

    return tuple(sorted(items, key=key))
