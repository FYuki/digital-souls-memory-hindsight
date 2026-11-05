"""Shared corpus selection and result contracts for execution and offline comparison."""

from collections.abc import Sequence
from typing import Literal, NotRequired, TypedDict

from .cases_contract import Case, required_case_ids
from .scoring import MetricCounts, TrialResult, summarize_trials

Suite = Literal["same-fact", "difference-fact", "japanese"]
SUITES: dict[Suite, str] = {
    "same-fact": "AC-M1",
    "difference-fact": "AC-M2",
    "japanese": "AC-R3",
}
GENERATION_MODELS = ("gemma4-12b", "gpt-6-luna")


class MemoryRow(TypedDict):
    id: str
    text: NotRequired[str | None]
    fact_type: NotRequired[str | None]
    document_id: NotRequired[str | None]
    source_memory_ids: NotRequired[list[str] | None]


class QualityTrial(TrialResult):
    suite: Suite
    trial: int
    reason: str
    elapsed_seconds: float
    failures: dict[str, str]
    memories: list[MemoryRow]


class QualityRun(TypedDict):
    verdict: str
    trials: list[QualityTrial]


def quality_suites(cases: list[Case]) -> dict[Suite, list[Case]]:
    required = required_case_ids(cases)
    return {
        suite: [
            case for case in cases if case["acceptance_id"] == acceptance and case["id"] in required
        ]
        for suite, acceptance in SUITES.items()
    }


def quality_verdict(trials: Sequence[QualityTrial], suites: dict[Suite, list[Case]]) -> str:
    passed = all(
        summarize_trials(
            [row for row in trials if row["suite"] == suite],
            required_case_ids={case["id"] for case in selected},
        )["verdict"]
        == "PASS"
        for suite, selected in suites.items()
    )
    return "PASS" if passed else "FAIL"


def empty_metric_counts() -> MetricCounts:
    return {
        "different_fact_pairs": 0,
        "merged_different_fact_pairs": 0,
        "same_fact_groups": 0,
        "unmerged_same_fact_groups": 0,
        "positive_queries": 0,
        "correct_positive_queries": 0,
        "negative_queries": 0,
        "empty_negative_queries": 0,
        "negative_returned_anchors": 0,
        "unrelated_negative_anchors": 0,
        "empty_fact_responses": 0,
        "empty_responses": 0,
    }
