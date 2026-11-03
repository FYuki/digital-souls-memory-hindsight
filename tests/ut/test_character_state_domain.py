"""Bounded, verbatim state inputs and gateway output validation."""

import importlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory_hindsight.evidence.domain import Turn, prepare_turn
from tests.ut.test_delivery_scope import CHARACTERS, NOW, draft

pytestmark = pytest.mark.ut


def source(identifier: str, age: int, text: str) -> tuple[Turn, str]:
    turn = prepare_turn(
        replace(draft(), turn_id=identifier, occurred_at=NOW + timedelta(seconds=age)),
        CHARACTERS,
        NOW,
    )
    return turn, text


def test_latest_twenty_are_returned_in_chronological_order() -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    inputs = tuple(source(f"t-{i:02}", i, f"架空{i}") for i in reversed(range(21)))
    selected = domain.select_input(inputs, max_chars=4000)
    assert [value.turn_id for value in selected] == [f"t-{i:02}" for i in range(1, 21)]
    assert [value.text for value in selected] == [f"架空{i}" for i in range(1, 21)]


def test_selection_uses_delivered_text_and_counts_unicode_codepoints() -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    inputs = (source("old", 0, "旧"), source("middle", 1, "茶🍵"), source("new", 2, "花🌸"))
    selected = domain.select_input(inputs, max_chars=4)
    assert [(value.turn_id, value.text) for value in selected] == [
        ("middle", "茶🍵"),
        ("new", "花🌸"),
    ]
    assert domain.select_input(inputs, max_chars=1) == ()


def test_first_oversized_turn_stops_selection_without_truncation_or_skipping() -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    selected = domain.select_input(
        (source("old", 0, "a"), source("large", 1, "long"), source("new", 2, "b")),
        max_chars=3,
    )
    assert [(value.turn_id, value.text) for value in selected] == [("new", "b")]


def test_empty_text_does_not_consume_turn_budget_and_ties_are_deterministic() -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    a, b = source("a", 0, "a"), source("b", 0, "b")
    c = (replace(a[0], turn_id="c", recorded_at=NOW + timedelta(seconds=1)), "c")
    selected = domain.select_input((c, b, a, source("empty", 2, "")), max_turns=2, max_chars=4)
    assert [value.turn_id for value in selected] == ["b", "c"]


@pytest.mark.parametrize(
    "max_turns,max_chars,reads,selected",
    [(20, 4000, 20, 20), (20, 4, 3, 2), (0, 4, 0, 0)],
)
def test_input_window_stops_consuming_at_decisive_candidate(
    max_turns: int, max_chars: int, reads: int, selected: int
) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    consumed: list[str] = []

    def candidates() -> Iterator[tuple[Turn, str]]:
        for i in reversed(range(100)):
            item = source(f"t-{i}", i, "茶🍵")
            consumed.append(item[0].turn_id)
            yield item

    window = domain.input_window(candidates(), max_turns=max_turns, max_chars=max_chars)
    assert len(consumed) == reads
    assert [t.turn_id for t, _ in window] == [f"t-{i}" for i in range(99, 99 - selected, -1)]


def test_input_window_skips_empty_candidates_without_using_the_budget() -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    empty, kept = source("empty", 1, ""), source("kept", 0, "茶")
    assert domain.input_window(iter((empty, kept)), max_turns=1, max_chars=1) == (kept,)


@pytest.mark.parametrize("field", ["mood", "relationship_temperature", "current_topic"])
@pytest.mark.parametrize("bad", [None, 3, True, [], "あ" * 201])
def test_each_output_field_must_be_a_bounded_string(field: str, bad: object) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    raw = {"mood": "穏やか", "relationship_temperature": "温かい", "current_topic": "お茶"}
    with pytest.raises(domain.InvalidStateOutput):
        domain.validate_output({**raw, field: bad})
    with pytest.raises(domain.InvalidStateOutput):
        domain.validate_output({key: value for key, value in raw.items() if key != field})


@pytest.mark.parametrize("raw", [None, [], "synthetic", 1])
def test_output_requires_a_mapping(raw: object) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    with pytest.raises(domain.InvalidStateOutput):
        domain.validate_output(raw)


def test_output_accepts_two_hundred_codepoints_and_discards_extra_fields() -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    assert domain.validate_output(
        {
            "mood": "🍵" * 200,
            "relationship_temperature": "温かい",
            "current_topic": "お茶",
            "extra": "must not be persisted",
        }
    ) == domain.CharacterState("🍵" * 200, "温かい", "お茶")


@pytest.mark.parametrize("elapsed,fresh", [(1799, True), (1800, True), (1801, False)])
def test_ttl_is_measured_from_successful_generation(elapsed: int, fresh: bool) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    assert domain.is_fresh(NOW, NOW + timedelta(seconds=elapsed), timedelta(seconds=1800)) is fresh
