"""Deterministic acceptance and separate measurements for recall performance."""

from importlib import import_module
from types import ModuleType

import pytest

pytestmark = pytest.mark.ut


@pytest.fixture
def performance() -> ModuleType:
    return import_module("tests.it2.performance")


def _conditions() -> dict[str, object]:
    return {
        "request_count": 200,
        "warmup_count": 20,
        "concurrency": 1,
        "query_length": 24,
        "models": ["gemma4-12b", "bge-m3", "bge-reranker-v2-m3"],
        "offload": "all",
        "context_length": 8192,
        "format": "assembled",
    }


def _samples() -> list[dict[str, object]]:
    return [
        {
            "elapsed_ms": 10000.0 if index < 20 else 600.0,
            "success": True,
            "positive": index % 2 == 0,
            "fact_nonempty": index % 2 == 0,
            "response_nonempty": index % 2 == 0,
            "warmup": index < 20,
        }
        for index in range(220)
    ]


def _series(performance: ModuleType, samples: list[dict[str, object]]) -> dict[str, object]:
    result: dict[str, object] = performance.summarize_series(
        samples=samples,
        conditions=_conditions(),
        counts={"sessions": 50, "input_facts": 50, "extracted_facts": 60, "consolidated_facts": 40},
        target_memories=100,
        preparation={"status": "complete"},
    )
    return result


@pytest.mark.parametrize("percentile,expected", [(50, 10.0), (95, 19.0), (100, 20.0)])
def test_percentile_uses_sorted_nearest_rank(
    performance: ModuleType, percentile: int, expected: float
) -> None:
    assert performance.percentile(list(range(20, 0, -1)), percentile) == expected


def test_percentile_of_empty_series_is_undefined(performance: ModuleType) -> None:
    assert performance.percentile([], 95) is None


def test_six_hundred_ms_passes_even_when_targets_are_missed(performance: ModuleType) -> None:
    result = _series(performance, _samples())
    assert result["verdict"] == "PASS"
    assert result["p50_ms"] == 600.0
    assert result["p95_ms"] == 600.0
    assert result["request_count"] == 200
    assert result["failure_rate"] == 0
    assert result["positive_nonempty_responses"] == 100
    assert result["target_p50_met"] is False
    assert result["target_p95_met"] is False
    assert result["conditions"] == _conditions()
    assert result["counts"] == {
        "sessions": 50,
        "input_facts": 50,
        "extracted_facts": 60,
        "consolidated_facts": 40,
    }


@pytest.mark.parametrize("failure", ["slow", "http", "all_empty", "negative_only_nonempty"])
def test_performance_rejects_failed_or_vacuous_measurements(
    performance: ModuleType, failure: str
) -> None:
    samples = _samples()
    if failure == "slow":
        for row in samples[20:]:
            row["elapsed_ms"] = 600.001
    elif failure == "http":
        samples[20]["success"] = False
    else:
        for row in samples:
            row["fact_nonempty"] = failure == "negative_only_nonempty" and not row["positive"]
            row["response_nonempty"] = row["fact_nonempty"]
    result = _series(performance, samples)
    assert result["verdict"] == "FAIL"
    if failure == "http":
        assert result["failure_rate"] == pytest.approx(1 / 200)


def test_empty_measurements_do_not_pass(performance: ModuleType) -> None:
    result = _series(performance, [])
    assert result["verdict"] == "FAIL"
    assert result["p95_ms"] is None


def test_counts_use_only_searchable_extracted_and_consolidated_memories(
    performance: ModuleType,
) -> None:
    rows = [
        {"id": str(i), "fact_type": kind}
        for i, kind in enumerate(["world", "experience", "observation", "opinion"])
    ]
    result = performance.count_memories(rows, sessions=2, input_facts=100)
    assert result == {
        "sessions": 2,
        "input_facts": 100,
        "extracted_facts": 2,
        "consolidated_facts": 1,
    }


def test_memory_target_cannot_be_replaced_by_input_fact_count(performance: ModuleType) -> None:
    result = performance.summarize_series(
        samples=_samples(),
        conditions=_conditions(),
        target_memories=100,
        preparation={"status": "complete"},
        counts={
            "sessions": 100,
            "input_facts": 100,
            "extracted_facts": 50,
            "consolidated_facts": 49,
        },
    )
    assert result["verdict"] == "FAIL"


def test_condition_changes_cannot_be_combined_into_one_pass(performance: ModuleType) -> None:
    first = _series(performance, _samples())
    second = performance.summarize_series(
        samples=_samples(),
        conditions={**_conditions(), "context_length": 4096},
        target_memories=1000,
        preparation={"status": "complete"},
        counts={
            "sessions": 500,
            "input_facts": 500,
            "extracted_facts": 600,
            "consolidated_facts": 400,
        },
    )
    result = performance.summarize_performance([first, second])
    assert result["verdict"] == "FAIL"
    assert result["series"] == [first, second]


@pytest.mark.parametrize(
    "before,after,expected",
    [(10, 10, 0), (10, 12, 2), (None, 12, None), (12, 10, None), (10, None, None)],
)
def test_generation_counter_distinguishes_zero_from_unmeasured(
    performance: ModuleType, before: int | None, after: int | None, expected: int | None
) -> None:
    result = performance.generation_delta(before=before, after=after, model="gemma4-12b")
    assert result["counter_delta"] == expected
    assert result["status"] == ("not_measured" if expected is None else "measured")


def test_local_counter_cannot_claim_luna_external_calls_are_zero(performance: ModuleType) -> None:
    result = performance.generation_delta(before=10, after=12, model="gpt-6-luna")
    assert result["counter_delta"] is None
    assert result["status"] == "not_measured"
    assert result["local_counter_delta"] == 2


@pytest.mark.parametrize("status", ["complete", "failed", "timeout"])
def test_preparation_verdict_is_shared_by_rows_and_overall(
    performance: ModuleType, status: str
) -> None:
    row = _series(performance, _samples())
    row["preparation"] = {"status": status}
    actual = performance.recompute_series(row)
    expected = "PASS" if status == "complete" else "FAIL"
    assert actual["verdict"] == expected
    second = {
        **row,
        "target_memories": 1000,
        "counts": {
            "sessions": 500,
            "input_facts": 500,
            "extracted_facts": 600,
            "consolidated_facts": 400,
        },
    }
    overall = performance.summarize_performance([row, second])
    assert overall["verdict"] == expected
    assert [series["verdict"] for series in overall["series"]] == [expected, expected]


def test_response_materials_cannot_replace_positive_facts(performance: ModuleType) -> None:
    samples = _samples()
    for sample in samples:
        sample["fact_nonempty"] = not sample["positive"]
        sample["response_nonempty"] = True
    result = _series(performance, samples)
    assert result["verdict"] == "FAIL"
    assert result["positive_fact_responses"] == 0
    assert result["fact_responses"] == 100
    assert result["positive_nonempty_responses"] == 100
    assert result["nonempty_responses"] == 200
    samples[20]["fact_nonempty"] = True
    assert _series(performance, samples)["verdict"] == "PASS"
