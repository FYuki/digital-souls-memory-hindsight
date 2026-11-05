"""Supervisor entries keep their results and reject repeated or mismatched runs."""

import json
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("module_name", ["test_recall_threshold", "test_recall_performance"])
def test_recall_entries_are_only_marked_it2(module_name: str) -> None:
    entry = import_module(f"tests.it2.{module_name}")
    marks = entry.pytestmark
    if not isinstance(marks, list):
        marks = [marks]
    assert {mark.name for mark in marks} == {"it2"}


@pytest.mark.parametrize("model", ["gemma4-12b", "gpt-6-luna"])
@pytest.mark.parametrize("mode", ["selection", "confirmation", "performance"])
def test_failed_recall_runs_are_saved_before_assertion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, model: str
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", model)
    monkeypatch.setenv("RECALL_MIN_RERANKER_SCORE", "0.65")
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic@postgres/ledger")
    monkeypatch.setenv("SERVICE_URL", "http://service:8000")
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight:8888")
    if mode == "performance":
        from tests.it2.performance import summarize_series

        entry = import_module("tests.it2.test_recall_performance")
        entry_function = entry.test_recall_performance
        runner_name = "run_performance"
        target = tmp_path / f"recall-performance-{model}.json"
        row = summarize_series(
            samples=[],
            conditions={
                "request_count": 200,
                "warmup_count": 20,
                "concurrency": 1,
                "query_length": 24,
                "models": [model],
                "offload": "all",
                "context_length": 8192,
                "format": "assembled",
            },
            counts={
                "sessions": 25,
                "input_facts": 25,
                "extracted_facts": None,
                "consolidated_facts": None,
            },
            target_memories=100,
            preparation={"status": "timeout", "waits": []},
        )
        payload = {"verdict": "FAIL", "series": [row]}
    else:
        entry = import_module("tests.it2.test_recall_threshold")
        entry_function = (
            entry.test_select_threshold if mode == "selection" else entry.test_confirm_threshold
        )
        runner_name = "run_abstention_trials"
        target = tmp_path / f"recall-threshold-{mode}-{model}.json"
        payload = {
            "verdict": "FAIL",
            "trials": [
                {
                    "bank_id": "synthetic-bank",
                    "completion": "timeout",
                    "cases": {},
                    "character_id": "it2-abstention-t1",
                    "queries": [],
                    "trial": 1,
                    "reason": "timeout",
                    "elapsed_seconds": 1.0,
                    "failures": {},
                    "metrics": None,
                }
            ],
        }
        if mode == "confirmation":
            (tmp_path / f"recall-threshold-selection-{model}.json").write_text(
                json.dumps(
                    {"model": model, "threshold": 0.65, "verdict": "PASS", "guideline_met": True}
                )
            )
    monkeypatch.setattr(entry, runner_name, Mock(return_value=payload))
    if mode == "selection":
        monkeypatch.setattr(
            entry,
            "select_threshold",
            Mock(return_value={"threshold": None, "verdict": "FAIL", "reason": "incomplete"}),
        )
    with pytest.raises(AssertionError):
        entry_function()
    saved = json.loads(target.read_text())
    assert saved["model"] == model
    assert saved["verdict"] == "FAIL"
    if mode == "performance":
        assert saved["series"] == payload["series"]
    else:
        assert saved["trials"] == payload["trials"]


def test_confirmation_threshold_mismatch_stops_before_ingestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", "gemma4-12b")
    monkeypatch.setenv("RECALL_MIN_RERANKER_SCORE", "0.5")
    selection = tmp_path / "recall-threshold-selection-gemma4-12b.json"
    selection.write_text(json.dumps({"model": "gemma4-12b", "threshold": 0.65, "verdict": "PASS"}))
    entry = import_module("tests.it2.test_recall_threshold")
    run = Mock()
    monkeypatch.setattr(entry, "run_abstention_trials", run)
    with pytest.raises(ValueError):
        entry.test_confirm_threshold()
    run.assert_not_called()
    assert not (tmp_path / "recall-threshold-confirmation-gemma4-12b.json").exists()


@pytest.mark.parametrize("progress", [False, True])
def test_existing_result_or_progress_stops_before_ingestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, progress: bool
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", "gemma4-12b")
    entry = import_module("tests.it2.test_recall_threshold")
    path = tmp_path / (
        "recall-threshold-selection-gemma4-12b.progress.jsonl"
        if progress
        else "recall-threshold-selection-gemma4-12b.json"
    )
    path.write_text('{"original": true}\n')
    run = Mock()
    monkeypatch.setattr(entry, "run_abstention_trials", run)
    with pytest.raises((ValueError, FileExistsError)):
        entry.test_select_threshold()
    assert path.read_text() == '{"original": true}\n'
    run.assert_not_called()
