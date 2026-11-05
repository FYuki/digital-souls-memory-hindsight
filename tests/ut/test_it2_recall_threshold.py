"""Threshold decisions use recorded scores and keep tuning separate from acceptance."""

from importlib import import_module
from types import ModuleType

import pytest

from tests.it2.quality_contract import empty_metric_counts
from tests.it2.scoring import MetricCounts, calculate_metrics

pytestmark = pytest.mark.ut


@pytest.fixture
def threshold() -> ModuleType:
    return import_module("tests.it2.threshold")


def _candidate(value: float, correct: int, empty: int, *, passed: bool) -> dict[str, object]:
    counts: MetricCounts = {
        **empty_metric_counts(),
        "positive_queries": 30,
        "correct_positive_queries": correct,
        "negative_queries": 30,
        "empty_negative_queries": empty,
    }
    return {
        "threshold": value,
        "completion": "complete",
        "required_passed": passed,
        "tuning_counts": counts,
        "metrics": calculate_metrics(**counts),
    }


def test_selection_maximizes_abstention_within_recall_guideline(threshold: ModuleType) -> None:
    rows = [
        _candidate(0.2, 30, 5, passed=True),
        _candidate(0.4, 27, 20, passed=True),
        _candidate(0.6, 26, 30, passed=True),
        _candidate(0.8, 30, 30, passed=False),
    ]
    selected = threshold.select_threshold(rows)
    assert selected["threshold"] == 0.4
    assert selected["guideline_met"] is True


def test_guideline_miss_is_explicit_and_prioritizes_recall(threshold: ModuleType) -> None:
    selected = threshold.select_threshold(
        [_candidate(0.3, 26, 10, passed=True), _candidate(0.6, 25, 30, passed=True)]
    )
    assert selected["threshold"] == 0.3
    assert selected["guideline_met"] is False


def test_no_passing_threshold_is_failure_without_default(threshold: ModuleType) -> None:
    selected = threshold.select_threshold([_candidate(0.5, 30, 30, passed=False)])
    assert selected["threshold"] is None
    assert selected["reason"] == "no_passing_threshold"
    assert selected["verdict"] == "FAIL"


def test_incomplete_trials_are_distinct_from_no_passing_threshold(threshold: ModuleType) -> None:
    row = {**_candidate(0.5, 30, 30, passed=True), "completion": "timeout"}
    selected = threshold.select_threshold([row])
    assert selected["threshold"] is None
    assert selected["reason"] == "incomplete"
    assert selected["verdict"] == "FAIL"


@pytest.mark.parametrize("reverse", [False, True])
def test_selection_ties_use_recall_then_lower_threshold(
    threshold: ModuleType, reverse: bool
) -> None:
    rows = [
        _candidate(0.2, 28, 20, passed=True),
        _candidate(0.4, 30, 20, passed=True),
        _candidate(0.3, 30, 20, passed=True),
    ]
    assert threshold.select_threshold(rows[::-1] if reverse else rows)["threshold"] == 0.3


def test_guideline_uses_counts_at_exact_ninety_percent(threshold: ModuleType) -> None:
    row = _candidate(0.5, 27, 30, passed=True)
    # A rounded display value must not control the integer boundary decision.
    row["metrics"] = {"recall": 0.899999999, "abstention_rate": 1.0}
    assert threshold.select_threshold([row])["guideline_met"] is True


def test_empty_candidate_table_cannot_select_a_threshold(threshold: ModuleType) -> None:
    result = threshold.select_threshold([])
    assert result["threshold"] is None
    assert result["verdict"] == "FAIL"
