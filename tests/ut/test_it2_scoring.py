"""Metrics and fixed-trial acceptance are separate contracts."""

from importlib import import_module
from types import ModuleType

import pytest

pytestmark = pytest.mark.ut


@pytest.fixture
def scoring() -> ModuleType:
    return import_module("tests.it2.scoring")


def test_metrics_use_the_defined_population_for_each_rate(scoring: ModuleType) -> None:
    result = scoring.calculate_metrics(
        different_fact_pairs=4,
        merged_different_fact_pairs=1,
        same_fact_groups=5,
        unmerged_same_fact_groups=2,
        positive_queries=10,
        correct_positive_queries=9,
        negative_queries=8,
        empty_negative_queries=6,
        negative_returned_anchors=5,
        unrelated_negative_anchors=2,
        empty_fact_responses=3,
        empty_responses=1,
    )
    assert result == {
        "false_merge_rate": 0.25,
        "missed_merge_rate": 0.4,
        "recall": 0.9,
        "abstention_rate": 0.75,
        "anchor_contamination_rate": 0.4,
        "empty_fact_responses": 3,
        "empty_responses": 1,
    }


def test_zero_population_is_undefined_instead_of_perfect_or_zero_quality(
    scoring: ModuleType,
) -> None:
    result = scoring.calculate_metrics(
        different_fact_pairs=0,
        merged_different_fact_pairs=0,
        same_fact_groups=0,
        unmerged_same_fact_groups=0,
        positive_queries=0,
        correct_positive_queries=0,
        negative_queries=0,
        empty_negative_queries=0,
        negative_returned_anchors=0,
        unrelated_negative_anchors=0,
        empty_fact_responses=0,
        empty_responses=0,
    )
    for name in (
        "false_merge_rate",
        "missed_merge_rate",
        "recall",
        "abstention_rate",
        "anchor_contamination_rate",
    ):
        assert result[name] is None


def _trials() -> list[dict[str, object]]:
    return [
        {
            "bank_id": f"b{i}",
            "completion": "complete",
            "cases": {"required": True},
            "metrics": {"false_merge_rate": 0.5, "recall": 0.8},
        }
        for i in range(1, 4)
    ]


def test_three_distinct_successes_pass_regardless_of_record_only_rates(scoring: ModuleType) -> None:
    trials = _trials()
    result = scoring.summarize_trials(trials, required_case_ids={"required"})
    assert result["verdict"] == "PASS"
    assert result["trials"] == trials


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_trial_count_is_exactly_three(scoring: ModuleType, count: int) -> None:
    trials = [*_trials(), {**_trials()[0], "bank_id": "b4"}][:count]
    assert scoring.summarize_trials(trials, required_case_ids={"required"})["verdict"] != "PASS"


def test_reused_bank_is_not_an_independent_trial(scoring: ModuleType) -> None:
    trials = _trials()
    trials[1] = {**trials[1], "bank_id": "b1"}
    assert scoring.summarize_trials(trials, required_case_ids={"required"})["verdict"] != "PASS"


@pytest.mark.parametrize("cases", [{"required": False}, {}, {"other": True}])
def test_earlier_failed_or_missing_case_cannot_be_replaced_by_last_success(
    scoring: ModuleType, cases: dict[str, bool]
) -> None:
    trials = _trials()
    trials[0] = {**trials[0], "cases": cases}
    result = scoring.summarize_trials(trials, required_case_ids={"required"})
    assert result["verdict"] != "PASS"
    assert result["trials"] == trials


@pytest.mark.parametrize("completion", ["waiting", "failed", "timeout"])
def test_unfinished_trial_has_no_quality_metrics(scoring: ModuleType, completion: str) -> None:
    trial = scoring.score_trial(
        bank_id="b1", completion=completion, cases={"required": True}, observations={}
    )
    assert trial["completion"] == completion
    assert trial["metrics"] is None
    trials = [trial, *_trials()[1:]]
    assert scoring.summarize_trials(trials, required_case_ids={"required"})["verdict"] != "PASS"


def test_completed_trial_calculates_and_records_metrics(scoring: ModuleType) -> None:
    counts = dict.fromkeys(
        (
            "different_fact_pairs",
            "merged_different_fact_pairs",
            "same_fact_groups",
            "unmerged_same_fact_groups",
            "positive_queries",
            "correct_positive_queries",
            "negative_queries",
            "empty_negative_queries",
            "negative_returned_anchors",
            "unrelated_negative_anchors",
            "empty_fact_responses",
            "empty_responses",
        ),
        0,
    )
    trial = scoring.score_trial(
        bank_id="b1",
        completion="complete",
        cases={"required": True},
        observations=counts,
    )
    assert trial["metrics"] == scoring.calculate_metrics(**counts)
    assert trial["bank_id"] == "b1"
    assert trial["cases"] == {"required": True}
