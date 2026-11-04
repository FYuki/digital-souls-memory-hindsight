"""Deterministic scoring exposed for resident-context selection."""

import importlib

import pytest

from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "query, text, expected",
    [
        ("abcd", "xabcy", 2),
        ("aaaa", "aaaaaa", 1),
        ("😀b🍵", "a😀b🍵c", 2),
        ("茶", "茶茶", 1),
        ("茶", "珈琲", 0),
    ],
)
def test_score_counts_shared_codepoint_grams(query: str, text: str, expected: int) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.voice.domain")
    assert domain.score(query, text) == expected


@pytest.mark.parametrize("query, expected", [(None, ["a", "b", "c"]), ("abcd", ["b", "c", "a"])])
def test_rank_is_reusable_and_uses_best_side_then_identifier(
    query: str | None, expected: list[str]
) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.voice.domain")
    items = tuple(
        domain.VoiceExemplar(
            character_id="char-a",
            exemplar_id=identifier,
            source="canonical_seed",
            answer=answer,
            question=question,
            answer_span=None,
            question_span=None,
            approved_by="synthetic-author",
            registered_at=NOW,
        )
        for identifier, answer, question in [
            ("c", "abcd", None),
            ("a", "ab", "ab"),
            ("b", "z", "abcd"),
        ]
    )
    assert [item.exemplar_id for item in domain.rank(items, query)] == expected
