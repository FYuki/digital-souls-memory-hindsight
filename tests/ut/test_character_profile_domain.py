"""Profile interval and budget rules, independent of persistence."""

import importlib
from dataclasses import replace

import pytest

from digital_souls_memory_hindsight.evidence.delivery import DeliveryScope
from digital_souls_memory_hindsight.evidence.domain import prepare_turn
from digital_souls_memory_hindsight.voice.domain import Span
from tests.ut.test_delivery_scope import CHARACTERS, NOW, draft

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "entries, limit",
    [
        ((), 0),
        ((Span("a", 0, 5),), 5),
        ((Span("a", 1, 4),), 3),
        ((Span("a", 2, 4), Span("a", 0, 2)), 4),
        ((Span("a", 0, 2), Span("b", 0, 2)), 4),
    ],
)
def test_accepts_exact_budget_adjacent_and_different_turn_intervals(
    entries: tuple[Span, ...], limit: int
) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_profile.domain")
    domain.validate_entries(entries, limit)


@pytest.mark.parametrize(
    "entries, limit",
    [
        ((Span("a", -1, 2),), 10),
        ((Span("a", 2, 2),), 10),
        ((Span("a", 3, 2),), 10),
        ((Span("a", 0, 3), Span("a", 2, 4)), 10),
        ((Span("a", 2, 4), Span("a", 0, 3)), 10),
        ((Span("a", 0, 4), Span("a", 1, 2)), 10),
        ((Span("a", 0, 2), Span("a", 0, 2)), 10),
        ((Span("a", 0, 2), Span("b", 0, 2)), 3),
    ],
)
def test_rejects_invalid_overlapping_or_over_budget_intervals(
    entries: tuple[Span, ...], limit: int
) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_profile.domain")
    with pytest.raises(domain.ProfileValidationError):
        domain.validate_entries(entries, limit)


@pytest.mark.parametrize(
    "span,character,limit,expected",
    [
        (Span("t-1", 0, 5), "char-a", 5, True),
        (Span("t-1", 1, 4), "char-a", 4, True),
        (Span("t-1", 0, 6), "char-a", 6, False),
        (Span("other", 0, 2), "char-a", 5, False),
        (Span("t-1", 0, 2), "char-c", 5, False),
        (Span("t-1", 1, 4), "char-a", 3, False),
        (Span("t-1", 0, 2), "char-a", None, False),
    ],
)
def test_entry_visibility_requires_matching_turn_body_perception_and_delivery(
    span: Span, character: str, limit: int | None, expected: bool
) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_profile.domain")
    turn = prepare_turn(replace(draft(), text="a😀b🍵c"), CHARACTERS, NOW)
    scope = DeliveryScope("t-1", "receipt_confirmed", limit, NOW)
    assert domain.entry_visible(turn, scope, character, span) == expected
    assert domain.entry_visible(turn, None, character, span) is False
