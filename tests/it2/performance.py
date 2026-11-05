"""Recall performance statistics retain samples and their measurement conditions."""

import math
from collections.abc import Sequence
from typing import NotRequired, TypedDict

from .quality_contract import MemoryRow

GENERATION_COUNTER = "llamacpp:n_decode_total"


class Conditions(TypedDict):
    request_count: int
    warmup_count: int
    concurrency: int
    query_length: int
    models: list[str]
    offload: str
    context_length: int
    format: str
    hindsight_generation_model: NotRequired[str]
    model_context_lengths: NotRequired[dict[str, int]]
    model_gpu_layers: NotRequired[dict[str, int]]


class MemoryCounts(TypedDict):
    sessions: int
    input_facts: int
    extracted_facts: int | None
    consolidated_facts: int | None


class Sample(TypedDict):
    elapsed_ms: float
    success: bool
    positive: bool
    fact_nonempty: bool
    response_nonempty: bool
    warmup: bool
    error: NotRequired[str]


class Series(TypedDict):
    verdict: str
    target_memories: int
    counts: MemoryCounts
    conditions: Conditions
    samples: list[Sample]
    request_count: int
    failure_rate: float | None
    positive_nonempty_responses: int
    nonempty_responses: int
    positive_fact_responses: int
    fact_responses: int
    p50_ms: float | None
    p95_ms: float | None
    target_p50_met: bool
    target_p95_met: bool
    preparation: dict[str, object]
    separate_measurements: NotRequired[dict[str, object]]
    generation: NotRequired[dict[str, object]]
    trace_durations: NotRequired[dict[str, float]]


def percentile(values: Sequence[float], percentile: int) -> float | None:
    if not 0 < percentile <= 100 or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Invalid percentile input")
    return sorted(values)[math.ceil(percentile / 100 * len(values)) - 1] if values else None


def count_memories(rows: Sequence[MemoryRow], *, sessions: int, input_facts: int) -> MemoryCounts:
    return {
        "sessions": sessions,
        "input_facts": input_facts,
        "extracted_facts": sum(row.get("fact_type") in {"world", "experience"} for row in rows),
        "consolidated_facts": sum(row.get("fact_type") == "observation" for row in rows),
    }


def summarize_series(
    *,
    samples: Sequence[Sample],
    conditions: Conditions,
    counts: MemoryCounts,
    target_memories: int,
    preparation: dict[str, object],
) -> Series:
    measured = [row for row in samples if not row["warmup"]]
    values = [row["elapsed_ms"] for row in measured]
    p50, p95 = percentile(values, 50), percentile(values, 95)
    failures = sum(not row["success"] for row in measured)
    positive_nonempty = sum(
        row["positive"] and row["response_nonempty"] and row["success"] for row in measured
    )
    positive_facts = sum(
        row["positive"] and row["fact_nonempty"] and row["success"] for row in measured
    )
    extracted, consolidated = counts["extracted_facts"], counts["consolidated_facts"]
    passed = (
        preparation.get("status") == "complete"
        and len(measured) == conditions["request_count"] > 0
        and sum(row["warmup"] for row in samples) == conditions["warmup_count"]
        and conditions["concurrency"] == 1
        and p95 is not None
        and p95 <= 600
        and failures == 0
        and positive_facts > 0
        and extracted is not None
        and consolidated is not None
        and extracted + consolidated >= target_memories
    )
    return {
        "verdict": "PASS" if passed else "FAIL",
        "target_memories": target_memories,
        "counts": {**counts},
        "conditions": {**conditions},
        "samples": list(samples),
        "preparation": {**preparation},
        "request_count": len(measured),
        "failure_rate": failures / len(measured) if measured else None,
        "positive_nonempty_responses": positive_nonempty,
        "nonempty_responses": sum(row["response_nonempty"] and row["success"] for row in measured),
        "positive_fact_responses": positive_facts,
        "fact_responses": sum(row["fact_nonempty"] and row["success"] for row in measured),
        "p50_ms": p50,
        "p95_ms": p95,
        "target_p50_met": p50 is not None and p50 <= 150,
        "target_p95_met": p95 is not None and p95 <= 300,
    }


def recompute_series(row: Series) -> Series:
    return {
        **row,
        **summarize_series(
            samples=row["samples"],
            conditions=row["conditions"],
            counts=row["counts"],
            target_memories=row["target_memories"],
            preparation=row["preparation"],
        ),
    }


def summarize_performance(series: Sequence[Series]) -> dict[str, object]:
    recomputed = [recompute_series(row) for row in series]
    passed = (
        len(series) == 2
        and [row["target_memories"] for row in series] == [100, 1000]
        and all(row["verdict"] == "PASS" for row in recomputed)
        and series[0]["conditions"] == series[1]["conditions"]
    )
    return {"verdict": "PASS" if passed else "FAIL", "series": recomputed}


def generation_delta(*, before: int | None, after: int | None, model: str) -> dict[str, object]:
    local_delta = (
        after - before if before is not None and after is not None and after >= before else None
    )
    if model != "gemma4-12b" or before is None or after is None or after < before:
        return {
            "status": "not_measured",
            "counter": GENERATION_COUNTER,
            "counter_delta": None,
            "local_counter_delta": local_delta,
            "reason": "counter_unavailable_or_external_model",
        }
    return {
        "status": "measured",
        "counter": GENERATION_COUNTER,
        "counter_delta": local_delta,
        "local_counter_delta": local_delta,
    }
