"""Rescoring stored trial evidence keeps results that cannot be recomputed."""

from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

from digital_souls_memory_hindsight.evidence.projection import document_id
from tests.it2.cases_contract import load_cases

pytestmark = pytest.mark.ut
CORPUS = Path(__file__).resolve().parents[1] / "it2/cases"


@pytest.fixture
def rescore() -> ModuleType:
    return import_module("tests.it2.rescore")


def _fact(identifier: str, text: str, character: str, session: str) -> dict[str, object]:
    return {
        "id": identifier,
        "text": text,
        "fact_type": "world",
        "document_id": document_id(f"{character}-{session}"),
    }


def _trial(
    suite: str, cases: dict[str, bool], memories: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "bank_id": "bank",
        "completion": "complete",
        "suite": suite,
        "trial": 1,
        "reason": "done",
        "elapsed_seconds": 1.0,
        "cases": cases,
        "failures": {key: "old" for key, passed in cases.items() if not passed},
        "metrics": {
            "false_merge_rate": 1.0,
            "missed_merge_rate": None,
            "recall": None,
            "abstention_rate": None,
            "anchor_contamination_rate": None,
            "empty_fact_responses": 0,
            "empty_responses": 0,
        },
        "memories": memories,
    }


def test_difference_trial_is_rescored_with_metrics(rescore: ModuleType) -> None:
    cases = load_cases(CORPUS)
    character = "it2-difference-fact-t1"
    memories: list[dict[str, object]] = [
        _fact("f1", "青い石は三個", character, "difference-number-s1"),
        _fact("f2", "青い石は五個", character, "difference-number-s1"),
        {
            "id": "o1",
            "text": "青い石は五個",
            "fact_type": "observation",
            "source_memory_ids": ["f1", "f2"],
        },
    ]
    row = _trial("difference-fact", {"difference-number-001": False}, memories)
    rescored = rescore.rescore_trial(row, cases)
    assert rescored["cases"]["difference-number-001"] is True
    assert rescored["failures"] == {}
    assert rescored["metrics"]["false_merge_rate"] == 0


def test_only_extracted_japanese_cases_are_rescored(rescore: ModuleType) -> None:
    cases = load_cases(CORPUS)
    character = "it2-japanese-t1"
    session = next(case for case in cases if case["id"] == "japanese-date-extracted_fact")["input"][
        "sessions"
    ][0]["session_id"]
    memories = [_fact("f1", "凪との約束 | When: 2026-11-05", character, session)]
    row = _trial(
        "japanese",
        {"japanese-date-extracted_fact": False, "japanese-date-returned_source": False},
        memories,
    )
    rescored = rescore.rescore_trial(row, cases)
    assert rescored["cases"]["japanese-date-extracted_fact"] is True
    assert rescored["cases"]["japanese-date-returned_source"] is False
    assert rescored["failures"] == {"japanese-date-returned_source": "old"}


def test_same_fact_and_incomplete_trials_are_unchanged(rescore: ModuleType) -> None:
    cases = load_cases(CORPUS)
    same = _trial("same-fact", {"same-fact-001": False}, [])
    assert rescore.rescore_trial(same, cases) == same
    incomplete = {
        **_trial("difference-fact", {"difference-number-001": False}, []),
        "completion": "failed",
        "metrics": None,
    }
    assert rescore.rescore_trial(incomplete, cases) == incomplete
