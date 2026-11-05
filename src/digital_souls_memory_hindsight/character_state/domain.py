"""State values, bounded verbatim inputs and gateway output validation."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..evidence.domain import Turn

DEFAULT_TTL_SECONDS = 1800
DEFAULT_INPUT_MAX_CHARS = 4000
MAX_OUTPUT_CHARS = 200


@dataclass(frozen=True)
class CharacterState:
    mood: str
    relationship_temperature: str
    current_topic: str


@dataclass(frozen=True)
class StateInput:
    turn_id: str
    actor: str
    kind: str
    text: str
    occurred_at: datetime


class InvalidStateOutput(ValueError):
    """The gateway did not return the three bounded state strings."""


def select_input(
    sources: Iterable[tuple[Turn, str]], *, max_turns: int = 20, max_chars: int
) -> tuple[StateInput, ...]:
    newest = sorted(
        sources,
        key=lambda source: (source[0].occurred_at, source[0].recorded_at, source[0].turn_id),
        reverse=True,
    )
    selected = input_window(newest, max_turns=max_turns, max_chars=max_chars)
    return tuple(
        StateInput(turn.turn_id, turn.actor, turn.kind, text, turn.occurred_at)
        for turn, text in reversed(selected)
    )


def input_window(
    newest_first: Iterable[tuple[Turn, str]], *, max_turns: int, max_chars: int
) -> tuple[tuple[Turn, str], ...]:
    selected: list[tuple[Turn, str]] = []
    length = 0
    if max_turns <= 0:
        return ()
    for turn, text in newest_first:
        if not text:
            continue
        if length + len(text) > max_chars:
            break
        selected.append((turn, text))
        length += len(text)
        if len(selected) == max_turns:
            break
    return tuple(selected)


def validate_output(raw: object) -> CharacterState:
    if not isinstance(raw, Mapping):
        raise InvalidStateOutput()
    values: list[str] = []
    for field in ("mood", "relationship_temperature", "current_topic"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_OUTPUT_CHARS:
            raise InvalidStateOutput()
        values.append(value)
    return CharacterState(*values)


def is_fresh(generated_at: datetime, now: datetime, ttl: timedelta) -> bool:
    return now - generated_at <= ttl
