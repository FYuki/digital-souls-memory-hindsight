"""Public reports produce trial-level comparisons without choosing a model."""

import json
import os
import re
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from tests.it2.cases_contract import load_cases
from tests.it2.report import generate_report
from tests.it2.scoring import MetricCounts, calculate_metrics

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def comparison() -> ModuleType:
    return import_module("tests.it2.comparison")


def _payload(model: str) -> dict[str, object]:
    cases = load_cases(ROOT / "tests/it2/cases")
    counts: MetricCounts = {
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
    trials = []
    for trial in range(1, 4):
        for suite, acceptance in (
            ("same-fact", "AC-M1"),
            ("difference-fact", "AC-M2"),
            ("japanese", "AC-R3"),
        ):
            measured: MetricCounts = {
                **counts,
                "different_fact_pairs": 4 if suite == "difference-fact" else 0,
                "same_fact_groups": 1 if suite == "same-fact" else 0,
            }
            trials.append(
                {
                    "suite": suite,
                    "trial": trial,
                    "bank_id": f"synthetic-{suite}-{trial}",
                    "completion": "complete",
                    "reason": "retain, consolidation and outbox completed",
                    "elapsed_seconds": 12.5 + trial,
                    "cases": {
                        case["id"]: True for case in cases if case["acceptance_id"] == acceptance
                    },
                    "failures": {},
                    "metrics": calculate_metrics(**measured),
                    "memories": [],
                }
            )
    return {
        "run_id": f"synthetic-{model}",
        "authorization": "supervisor; synthetic only",
        "environment": {"gpu": "synthetic GPU"},
        "revision": "a" * 40,
        "timestamp": "2026-11-05T00:00:00Z",
        "models": {"generation": model},
        "images": {"hindsight": "sha256:" + "1" * 64},
        "settings": {"recall_min_reranker_score": 0.5},
        "scorer_version": "1",
        "completion": "complete",
        "trials": trials,
        "costs_and_constraints": "fixture only; no real model run",
        "synthetic_inputs": [],
    }


def _rows(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], payload["trials"])


def _failed(payload: dict[str, object], trial: int) -> dict[str, object]:
    row = next(
        row for row in _rows(payload) if row["suite"] == "japanese" and row["trial"] == trial
    )
    row["cases"] = {**cast(dict[str, bool], row["cases"]), "japanese-date-extracted_fact": False}
    row["failures"] = {"japanese-date-extracted_fact": "extracted_fact_not_preserved"}
    return payload


def _table_rows(text: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.strip().startswith("|")
    ]


def test_comparison_shows_both_models_each_trial_and_failure_breakdown(
    comparison: ModuleType,
    tmp_path: Path,
) -> None:
    gemma = _payload("gemma4-12b")
    # Rates are record-only: the verdict must still follow the mandatory cases.
    cast(dict[str, object], _rows(gemma)[1]["metrics"])["false_merge_rate"] = 0.25
    paths = [
        generate_report(payload, tmp_path / "reports")
        for payload in (gemma, _failed(_payload("gpt-6-luna"), 2))
    ]
    output = tmp_path / "comparison.md"
    comparison.generate_comparison(paths, output)
    text = output.read_text()
    rows = _table_rows(text)
    assert any("gemma4-12b" in row and "PASS" in row for row in rows)
    assert any("gpt-6-luna" in row and "FAIL" in row for row in rows)
    for model in ("gemma4-12b", "gpt-6-luna"):
        for trial in range(1, 4):
            for suite in ("same-fact", "difference-fact", "japanese"):
                assert any(model in row and suite in row and str(trial) in row for row in rows)
    assert "extracted_fact_not_preserved" in text
    assert "japanese-date-extracted_fact" in text
    assert all(str(12.5 + trial) in text for trial in range(1, 4))
    assert "未定義" in text
    assert any(re.fullmatch(r"(?:0\.25|25(?:\.0+)?\s*%)", cell) for row in rows for cell in row)


def test_missing_model_is_not_run(comparison: ModuleType, tmp_path: Path) -> None:
    path = generate_report(_payload("gemma4-12b"), tmp_path / "reports")
    output = tmp_path / "comparison.md"
    comparison.generate_comparison([path], output)
    rows = [row for row in _table_rows(output.read_text()) if "gpt-6-luna" in row]
    assert rows
    assert all("NOT RUN" in row and "PASS" not in row for row in rows)


def test_reported_pass_is_recomputed_from_all_trials(
    comparison: ModuleType, tmp_path: Path
) -> None:
    payload = _failed(_payload("gemma4-12b"), 3)
    # A stale summary is not authoritative; the case outcomes own acceptance.
    _rows(payload)[-1]["verdict"] = "PASS"
    path = generate_report(payload, tmp_path / "reports")
    output = tmp_path / "comparison.md"
    comparison.generate_comparison([path], output)
    assert any("gemma4-12b" in row and "FAIL" in row for row in _table_rows(output.read_text()))


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate",
        "version",
        "case_set",
        "missing_trial",
        "string_bool",
        "unsafe",
        "completion",
        "elapsed",
        "bank",
        "metrics",
        "rate",
        "failure_details",
    ],
)
def test_invalid_comparison_is_rejected_without_output(
    comparison: ModuleType,
    tmp_path: Path,
    invalid: str,
) -> None:
    first = _payload("gemma4-12b")
    second = _payload("gpt-6-luna")
    if invalid == "duplicate":
        second["models"] = {"generation": "gemma4-12b"}
    elif invalid == "version":
        second["scorer_version"] = "different-version"
    elif invalid == "case_set":
        _rows(second)[0]["cases"] = {"unexpected-case": True}
    elif invalid == "missing_trial":
        second["trials"] = _rows(second)[1:]
    elif invalid == "string_bool":
        _rows(second)[0]["cases"] = {"same-fact-001": "true"}
    elif invalid == "unsafe":
        _rows(second)[0]["reason"] = "Bearer synthetic-credential"
    elif invalid == "completion":
        _rows(second)[0]["completion"] = "unknown"
    elif invalid == "elapsed":
        _rows(second)[0]["elapsed_seconds"] = -1
    elif invalid == "bank":
        _rows(second)[1]["bank_id"] = _rows(second)[0]["bank_id"]
    elif invalid == "metrics":
        _rows(second)[0]["metrics"] = None
    elif invalid == "rate":
        cast(dict[str, object], _rows(second)[0]["metrics"])["missed_merge_rate"] = 1.1
    else:
        _failed(second, 2)
        next(row for row in _rows(second) if row["suite"] == "japanese" and row["trial"] == 2)[
            "failures"
        ] = {}
    paths = []
    for index, payload in enumerate((first, second)):
        path = tmp_path / f"input-{index}.json"
        path.write_text(json.dumps(payload))
        paths.append(path)
    output = tmp_path / "comparison.md"
    with pytest.raises(ValueError):
        comparison.generate_comparison(paths, output)
    assert not output.exists()


def test_unfinished_trial_has_unmeasured_metrics(comparison: ModuleType, tmp_path: Path) -> None:
    payload = _payload("gemma4-12b")
    _rows(payload)[0].update(
        completion="timeout", metrics=None, reason="completion wait limit exceeded"
    )
    path = generate_report(payload, tmp_path / "reports")
    output = tmp_path / "comparison.md"
    comparison.generate_comparison([path], output)
    assert "未測定" in output.read_text()
    assert any("gemma4-12b" in row and "FAIL" in row for row in _table_rows(output.read_text()))


def test_comparison_cli_creates_output_and_refuses_overwrite(
    comparison: ModuleType,
    tmp_path: Path,
) -> None:
    path = generate_report(_payload("gemma4-12b"), tmp_path / "reports")
    output = tmp_path / "comparison.md"
    args = [
        sys.executable,
        "-m",
        comparison.__name__,
        "--report",
        str(path),
        "--output",
        str(output),
    ]
    first = subprocess.run(
        args,
        cwd=ROOT,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert first.returncode == 0, first.stderr
    original = output.read_text()
    second = subprocess.run(
        args,
        cwd=ROOT,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert second.returncode != 0
    assert output.read_text() == original


@pytest.mark.parametrize("count", [1, 3])
def test_progress_records_cannot_be_used_as_comparison_reports(
    comparison: ModuleType, tmp_path: Path, count: int
) -> None:
    progress = tmp_path / "extraction-quality-gemma4-12b.progress.jsonl"
    progress.write_text(
        "".join(json.dumps(row) + "\n" for row in _rows(_payload("gemma4-12b"))[:count])
    )
    output = tmp_path / "comparison.md"
    with pytest.raises(ValueError):
        comparison.generate_comparison([progress], output)
    assert not output.exists()
