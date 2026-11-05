"""Offline report generation never converts absent or failed evidence into PASS."""

import json
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

from digital_souls_memory_hindsight.evidence.projection import document_id
from tests.it2.cases_contract import load_cases
from tests.it2.scoring import SCORER_VERSION

pytestmark = pytest.mark.ut
CORPUS = Path(__file__).resolve().parents[1] / "it2/cases"


@pytest.fixture
def recall_report() -> ModuleType:
    return import_module("tests.it2.recall_report")


def test_missing_runs_are_reported_as_not_run(recall_report: ModuleType, tmp_path: Path) -> None:
    output = tmp_path / "recall.md"
    recall_report.generate_recall_report([], output)
    text = output.read_text()
    assert "NOT RUN" in text
    assert "gemma4-12b" in text
    assert "gpt-6-luna" in text
    assert "PASS" not in text


def test_report_recomputes_confirmation_from_all_three_trials(
    recall_report: ModuleType, tmp_path: Path
) -> None:
    cases = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    required = {
        case["id"]: True
        for case in load_cases(CORPUS)
        if case["acceptance_id"] == "AC-R4" and case["split"] == "final"
    }
    failed_id = next(iter(required))
    counts = {
        "recall": 1.0,
        "abstention_rate": 1.0,
        "anchor_contamination_rate": None,
        "false_merge_rate": None,
        "missed_merge_rate": None,
        "empty_fact_responses": 10,
        "empty_responses": 0,
    }
    trials = [
        {
            "bank_id": f"bank-{trial}",
            "completion": "complete",
            "cases": {**required, **({failed_id: False} if trial == 2 else {})},
            "metrics": counts,
            "trial": trial,
            "character_id": f"it2-abstention-t{trial}",
            "reason": "synthetic",
            "elapsed_seconds": 1.0,
            "failures": {},
            "queries": [
                {
                    "case_id": case["id"],
                    "candidates": [],
                    "response": {
                        "memories": [
                            {
                                "id": "f",
                                "source_turn_ids": [
                                    f"it2-abstention-t{trial}-{case['input']['sessions'][0]['mentions'][0]['id']}"
                                ],
                            }
                        ]
                        if case["positive"] and not (trial == 2 and case["id"] == failed_id)
                        else [],
                        "source_spans": [],
                        "verbatim_anchors": [],
                        "context": None,
                    },
                }
                for case in cases
            ],
        }
        for trial in range(1, 4)
    ]
    source = tmp_path / "recall-threshold-confirmation-gemma4-12b.json"
    source.write_text(
        json.dumps(
            {
                "model": "gemma4-12b",
                "scorer_version": SCORER_VERSION,
                "verdict": "PASS",
                "threshold": 0.65,
                "trials": trials,
            }
        )
    )
    output = tmp_path / "recall.md"
    recall_report.generate_recall_report([source], output)
    text = output.read_text()
    assert "FAIL" in text
    assert failed_id in text
    assert "未定義" in text
    assert "0.65" in text


def test_existing_public_report_is_never_overwritten(
    recall_report: ModuleType, tmp_path: Path
) -> None:
    output = tmp_path / "recall.md"
    output.write_text("original synthetic report\n")
    with pytest.raises(FileExistsError):
        recall_report.generate_recall_report([], output)
    assert output.read_text() == "original synthetic report\n"


@pytest.mark.parametrize("unsafe", ["Bearer synthetic-credential", "/home/synthetic/private"])
def test_unsafe_evidence_is_rejected_before_public_output(
    recall_report: ModuleType, tmp_path: Path, unsafe: str
) -> None:
    source = tmp_path / "recall-threshold-selection-gemma4-12b.json"
    source.write_text(
        json.dumps(
            {
                "model": "gemma4-12b",
                "reason": unsafe,
                "verdict": "FAIL",
                "threshold": None,
                "trials": [],
            }
        )
    )
    output = tmp_path / "recall.md"
    with pytest.raises(ValueError):
        recall_report.generate_recall_report([source], output)
    assert not output.exists()


def test_selection_table_is_rebuilt_from_candidate_scores(
    recall_report: ModuleType, tmp_path: Path
) -> None:
    cases = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    trials = []
    for trial in range(1, 4):
        character = f"it2-abstention-t{trial}"
        queries = []
        for case in cases:
            session = case["input"]["sessions"][0]
            queries.append(
                {
                    "case_id": case["id"],
                    "response": {"memories": [], "source_spans": [], "verbatim_anchors": []},
                    "candidates": [
                        {
                            "id": case["id"],
                            "kind": "world",
                            "text": session["mentions"][0]["text"],
                            "reranker_score": 0.8 if case["positive"] else 0.4,
                            "sources": [
                                {
                                    "document_id": document_id(
                                        f"{character}-{session['session_id']}"
                                    ),
                                    "chunk_text": session["mentions"][0]["text"],
                                }
                            ],
                        }
                    ],
                }
            )
        trials.append(
            {
                "character_id": character,
                "bank_id": f"bank-{trial}",
                "completion": "complete",
                "trial": trial,
                "reason": "synthetic",
                "elapsed_seconds": 1.0,
                "queries": queries,
                "cases": {},
                "failures": {},
                "metrics": None,
            }
        )
    source = tmp_path / "recall-threshold-selection-gemma4-12b.json"
    source.write_text(
        json.dumps(
            {
                "model": "gemma4-12b",
                "scorer_version": SCORER_VERSION,
                "trials": trials,
                "threshold": 0.99,
                "verdict": "FAIL",
            }
        )
    )
    output = tmp_path / "selection.md"
    recall_report.generate_recall_report([source], output)
    text = output.read_text()
    assert "選択値: 0.45" in text
    assert "| 0.45 | 1.0 | 1.0 | True |" in text
    assert "| gemma4-12b | threshold-selection | PASS |" in text


@pytest.mark.parametrize("failure", ["http", "facts", "preparation", "none"])
def test_performance_table_recomputes_failure_from_samples(
    recall_report: ModuleType, tmp_path: Path, failure: str
) -> None:
    from tests.it2.performance import Conditions, Sample, summarize_series

    conditions: Conditions = {
        "request_count": 200,
        "warmup_count": 20,
        "concurrency": 1,
        "query_length": 24,
        "models": ["gemma4-12b"],
        "offload": "999",
        "context_length": 32768,
        "format": "assembled",
    }
    samples: list[Sample] = [
        {
            "elapsed_ms": 600.0,
            "success": True,
            "positive": i % 2 == 0,
            "fact_nonempty": True,
            "response_nonempty": True,
            "warmup": i < 20,
        }
        for i in range(220)
    ]
    series = []
    for target in (100, 1000):
        row = summarize_series(
            samples=samples,
            conditions=conditions,
            target_memories=target,
            preparation={"status": "complete"},
            counts={
                "sessions": target,
                "input_facts": target,
                "extracted_facts": target,
                "consolidated_facts": 0,
            },
        )
        series.append(row)
    if failure == "http":
        series[1]["samples"][20] = {**samples[20], "success": False}
    elif failure == "facts":
        series[1]["samples"] = [{**sample, "fact_nonempty": False} for sample in samples]
    elif failure == "preparation":
        series[1]["preparation"] = {"status": "failed"}
    source = tmp_path / "recall-performance-gemma4-12b.json"
    source.write_text(
        json.dumps(
            {
                "model": "gemma4-12b",
                "scorer_version": SCORER_VERSION,
                "verdict": "PASS",
                "series": series,
            }
        )
    )
    output = tmp_path / "performance.md"
    recall_report.generate_recall_report([source], output)
    text = output.read_text()
    expected = "PASS" if failure == "none" else "FAIL"
    assert f"| gemma4-12b | performance | {expected} |" in text
    assert f"| 1000 | {expected} |" in text
    assert "正例Fact非空 | 正例全体非空 | Fact非空 | 全体非空" in text
    if failure == "http":
        assert "0.005" in text


def test_performance_evidence_requires_preparation_and_new_sample_fields() -> None:
    from pydantic import TypeAdapter

    from tests.it2.performance import Conditions, Series, summarize_series
    from tests.ut.test_it2_performance import _conditions, _samples

    adapter: TypeAdapter[Series] = TypeAdapter(Series)
    row = summarize_series(
        samples=[],
        conditions=TypeAdapter(Conditions).validate_python(_conditions()),
        counts={"sessions": 1, "input_facts": 1, "extracted_facts": 100, "consolidated_facts": 0},
        target_memories=100,
        preparation={"status": "complete"},
    )
    adapter.validate_python(row)
    missing = dict(row)
    del missing["preparation"]
    with pytest.raises(ValueError):
        adapter.validate_python(missing)
    old_samples = [{**sample, "nonempty": sample["response_nonempty"]} for sample in _samples()]
    for sample in old_samples:
        del sample["fact_nonempty"]
        del sample["response_nonempty"]
    with pytest.raises(ValueError):
        adapter.validate_python({**row, "samples": old_samples})


def test_report_cli_preserves_existing_output(
    recall_report: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.md"
    monkeypatch.setattr("sys.argv", ["recall_report", "--output", str(output)])
    assert recall_report.main() == 0
    original = output.read_text()
    assert recall_report.main() == 1
    assert output.read_text() == original
