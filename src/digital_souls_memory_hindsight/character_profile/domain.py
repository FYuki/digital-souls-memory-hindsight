"""Character profile values and original interval constraints."""

from dataclasses import dataclass
from datetime import datetime

from ..evidence.delivery import DeliveryScope
from ..evidence.domain import Turn
from ..voice.domain import Span, span_visible


class ProfileValidationError(Exception):
    """The profile contains invalid or unavailable original intervals."""


@dataclass(frozen=True)
class CharacterProfile:
    character_id: str
    char_limit: int
    revision: int
    entries: tuple[Span, ...]
    updated_at: datetime


def validate_entries(entries: tuple[Span, ...], char_limit: int) -> None:
    if char_limit < 0 or any(not 0 <= entry.start < entry.end for entry in entries):
        raise ProfileValidationError()
    ordered = sorted(entries, key=lambda entry: (entry.turn_id, entry.start, entry.end))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.turn_id == current.turn_id and previous.end > current.start:
            raise ProfileValidationError()
    if sum(entry.end - entry.start for entry in entries) > char_limit:
        raise ProfileValidationError()


def entry_visible(turn: Turn, scope: DeliveryScope | None, character_id: str, span: Span) -> bool:
    return (
        turn.turn_id == span.turn_id
        and span.end <= len(turn.text)
        and span_visible(turn, scope, character_id, span)
    )
