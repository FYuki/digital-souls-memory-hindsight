"""Evaluate recorded reranker candidates and choose a fixed threshold."""

from collections.abc import Mapping, Sequence
from typing import TypedDict, cast

from digital_souls_memory_hindsight.evidence.projection import document_id

from .cases_contract import Case
from .quality import QualityResult, RecallData, RecalledMemory, response_counts
from .quality_contract import empty_metric_counts
from .scoring import (
    MetricCounts,
    Metrics,
    TrialResult,
    calculate_metrics,
    score_trial,
    summarize_trials,
)

THRESHOLDS = tuple(index / 20 for index in range(21))


class CandidateSource(TypedDict):
    document_id: str
    chunk_text: str


class Candidate(TypedDict):
    id: str
    kind: str | None
    text: str
    reranker_score: float | None
    sources: list[CandidateSource]


class QueryRecord(TypedDict):
    case_id: str
    candidates: list[Candidate]
    response: RecallData


class AbstentionTrial(TrialResult):
    character_id: str
    trial: int
    reason: str
    elapsed_seconds: float
    queries: list[QueryRecord]
    failures: dict[str, str]


class CandidateEvaluation(TypedDict):
    threshold: float
    completion: str
    required_passed: bool
    tuning_counts: MetricCounts
    metrics: Metrics
    trials: list[TrialResult]


class Selection(TypedDict):
    threshold: float | None
    guideline_met: bool
    verdict: str
    reason: str


def filter_candidates(candidates: Sequence[Candidate], *, threshold: float) -> list[Candidate]:
    return [
        row
        for row in candidates
        if row["kind"] in {"world", "experience", "observation"}
        and row["reranker_score"] is not None
        and row["reranker_score"] >= threshold
        and row["sources"]
    ]


def score_responses(
    *, cases: Sequence[Case], character_id: str, recalls: dict[str, RecallData]
) -> QualityResult:
    outcomes: dict[str, bool] = {}
    counts = empty_metric_counts()
    failures: dict[str, str] = {}
    for case in cases:
        recall = recalls[case["id"]]
        for key, value in response_counts(recall).items():
            counts[key] += value
        if case["positive"]:
            expected_turns = {
                f"{character_id}-{mention['id']}"
                for other in cases
                for session in other["input"]["sessions"]
                for mention in session["mentions"]
                if mention["text"] in case["expected"]["facts"]
            }
            returned = {turn for memory in recall["memories"] for turn in memory["source_turn_ids"]}
            passed = bool(expected_turns & returned)
            counts["positive_queries"] += 1
            counts["correct_positive_queries"] += int(passed)
        else:
            passed = not recall["memories"]
            counts["negative_queries"] += 1
            counts["empty_negative_queries"] += int(passed)
            anchors = len(recall.get("verbatim_anchors", []))
            counts["negative_returned_anchors"] += anchors
            counts["unrelated_negative_anchors"] += anchors
        outcomes[case["id"]] = passed
        if not passed:
            failures[case["id"]] = (
                "positive_not_recalled" if case["positive"] else "negative_not_empty"
            )
    return {"cases": outcomes, "failures": failures, "observations": counts}


def evaluate_candidates(
    *, cases: list[Case], trials: Sequence[AbstentionTrial], thresholds: Sequence[float]
) -> list[CandidateEvaluation]:
    required = {case["id"] for case in cases if case["split"] == "final"}
    evaluations: list[CandidateEvaluation] = []
    for threshold in thresholds:
        total = empty_metric_counts()
        tuning_trials: list[TrialResult] = []
        required_trials: list[TrialResult] = []
        for trial in trials:
            recalls: dict[str, RecallData] = {}
            for query in trial["queries"]:
                turns_by_doc = {
                    document_id(f"{trial['character_id']}-{session['session_id']}"): [
                        f"{trial['character_id']}-{mention['id']}"
                        for mention in session["mentions"]
                    ]
                    for case in cases
                    for session in case["input"]["sessions"]
                }
                memories: list[RecalledMemory] = [
                    {
                        "id": row["id"],
                        "source_turn_ids": [
                            turn
                            for source in row["sources"]
                            for turn in turns_by_doc.get(source["document_id"], [])
                        ],
                    }
                    for row in filter_candidates(query["candidates"], threshold=threshold)
                ]
                recalls[query["case_id"]] = {
                    "memories": memories,
                    "source_spans": [],
                    "verbatim_anchors": query["response"].get("verbatim_anchors", []),
                }
            complete = (
                trial["completion"] == "complete"
                and len(trial["queries"]) == len(cases)
                and set(recalls) == {c["id"] for c in cases}
            )
            scored: QualityResult = (
                score_responses(cases=cases, character_id=trial["character_id"], recalls=recalls)
                if complete
                else {
                    "cases": {c["id"]: False for c in cases},
                    "failures": {c["id"]: "incomplete" for c in cases},
                    "observations": empty_metric_counts(),
                }
            )
            required_trials.append(
                score_trial(
                    bank_id=trial["bank_id"],
                    completion="complete" if complete else "failed",
                    cases=scored["cases"],
                    observations=scored["observations"],
                )
            )
            tuning_cases = [case for case in cases if case["split"] == "tuning"]
            # Score with the full lineage corpus; aggregate only tuning observations.
            counts = empty_metric_counts()
            if complete:
                for case in tuning_cases:
                    positive = case["positive"]
                    counts["positive_queries" if positive else "negative_queries"] += 1
                    counts[
                        "correct_positive_queries" if positive else "empty_negative_queries"
                    ] += int(scored["cases"][case["id"]])
                    for key, value in response_counts(recalls[case["id"]]).items():
                        counts[key] += value
                    if not positive:
                        anchors = len(recalls[case["id"]].get("verbatim_anchors", []))
                        counts["negative_returned_anchors"] += anchors
                        counts["unrelated_negative_anchors"] += anchors
            total_map = cast(Mapping[str, int], total)
            counts_map = cast(Mapping[str, int], counts)
            total = cast(
                MetricCounts, {name: value + counts_map[name] for name, value in total_map.items()}
            )
            tuning_trials.append(
                score_trial(
                    bank_id=trial["bank_id"],
                    completion="complete" if complete else "failed",
                    cases={c["id"]: scored["cases"][c["id"]] for c in tuning_cases},
                    observations=counts,
                )
            )
        evaluations.append(
            {
                "threshold": threshold,
                "completion": "complete"
                if len(trials) == 3 and all(t["completion"] == "complete" for t in required_trials)
                else "incomplete",
                "required_passed": summarize_trials(required_trials, required_case_ids=required)[
                    "verdict"
                ]
                == "PASS",
                "tuning_counts": total,
                "metrics": calculate_metrics(**total),
                "trials": tuning_trials,
            }
        )
    return evaluations


def select_threshold(rows: Sequence[CandidateEvaluation]) -> Selection:
    if not rows or any(row["completion"] != "complete" for row in rows):
        return {
            "threshold": None,
            "guideline_met": False,
            "verdict": "FAIL",
            "reason": "incomplete",
        }
    passing = [row for row in rows if row["required_passed"]]
    if not passing:
        return {
            "threshold": None,
            "guideline_met": False,
            "verdict": "FAIL",
            "reason": "no_passing_threshold",
        }
    eligible = [
        row
        for row in passing
        if row["tuning_counts"]["positive_queries"] > 0
        and row["tuning_counts"]["correct_positive_queries"] * 10
        >= row["tuning_counts"]["positive_queries"] * 9
    ]

    def rank(row: CandidateEvaluation) -> tuple[float, float, float]:
        metrics = calculate_metrics(**row["tuning_counts"])
        recall, abstention = metrics["recall"], metrics["abstention_rate"]
        if recall is None or abstention is None:
            raise ValueError("Threshold ranking requires both tuning populations")
        return (
            (abstention, recall, -row["threshold"])
            if eligible
            else (recall, abstention, -row["threshold"])
        )

    chosen = max(eligible or passing, key=rank)
    return {
        "threshold": chosen["threshold"],
        "guideline_met": bool(eligible),
        "verdict": "PASS",
        "reason": "selected",
    }
