"""Record quality metrics separately from mandatory-case acceptance."""

from collections.abc import Sequence, Set
from typing import TypedDict

SCORER_VERSION = "1"


class MetricCounts(TypedDict):
    different_fact_pairs: int
    merged_different_fact_pairs: int
    same_fact_groups: int
    unmerged_same_fact_groups: int
    positive_queries: int
    correct_positive_queries: int
    negative_queries: int
    empty_negative_queries: int
    negative_returned_anchors: int
    unrelated_negative_anchors: int
    empty_fact_responses: int
    empty_responses: int


class Metrics(TypedDict):
    false_merge_rate: float | None
    missed_merge_rate: float | None
    recall: float | None
    abstention_rate: float | None
    anchor_contamination_rate: float | None
    empty_fact_responses: int
    empty_responses: int


class TrialResult(TypedDict):
    bank_id: str
    completion: str
    cases: dict[str, bool]
    metrics: Metrics | None


def _rate(part: int, total: int) -> float | None:
    if not 0 <= part <= total:
        raise ValueError("Metric counts must satisfy 0 <= part <= total")
    return part / total if total else None


def calculate_metrics(
    *,
    different_fact_pairs: int,
    merged_different_fact_pairs: int,
    same_fact_groups: int,
    unmerged_same_fact_groups: int,
    positive_queries: int,
    correct_positive_queries: int,
    negative_queries: int,
    empty_negative_queries: int,
    negative_returned_anchors: int,
    unrelated_negative_anchors: int,
    empty_fact_responses: int,
    empty_responses: int,
) -> Metrics:
    return {
        "false_merge_rate": _rate(merged_different_fact_pairs, different_fact_pairs),
        "missed_merge_rate": _rate(unmerged_same_fact_groups, same_fact_groups),
        "recall": _rate(correct_positive_queries, positive_queries),
        "abstention_rate": _rate(empty_negative_queries, negative_queries),
        "anchor_contamination_rate": _rate(unrelated_negative_anchors, negative_returned_anchors),
        "empty_fact_responses": empty_fact_responses,
        "empty_responses": empty_responses,
    }


def score_trial(
    *,
    bank_id: str,
    completion: str,
    cases: dict[str, bool],
    observations: MetricCounts,
) -> TrialResult:
    return {
        "bank_id": bank_id,
        "completion": completion,
        "cases": dict(cases),
        "metrics": calculate_metrics(**observations) if completion == "complete" else None,
    }


def summarize_trials(
    trials: Sequence[TrialResult],
    *,
    required_case_ids: Set[str],
) -> dict[str, object]:
    passed = (
        len(trials) == 3
        and len({trial["bank_id"] for trial in trials}) == 3
        and bool(required_case_ids)
        and all(
            trial["completion"] == "complete"
            and all(trial["cases"].get(identifier) is True for identifier in required_case_ids)
            for trial in trials
        )
    )
    return {"verdict": "PASS" if passed else "FAIL", "trials": list(trials)}
