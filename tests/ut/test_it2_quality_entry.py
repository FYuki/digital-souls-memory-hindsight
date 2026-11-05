"""IT2 entry persists failure evidence; its model runner is never executed here."""

import json
from importlib import import_module
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from tests.it2.completion import CompletionResult
from tests.it2.quality_run import IngestPlan

pytestmark = pytest.mark.ut


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic@postgres/ledger")
    monkeypatch.setenv("SERVICE_URL", "http://service:8000")
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight:8888")
    return import_module("tests.it2.test_extraction_quality")


def test_real_model_entry_is_only_marked_it2(entry: ModuleType) -> None:
    marks = entry.pytestmark
    if not isinstance(marks, list):
        marks = [marks]
    assert {mark.name for mark in marks} == {"it2"}


@pytest.mark.parametrize("model", ["gemma4-12b", "gpt-6-luna"])
def test_failed_run_is_saved_before_the_it2_assertion(
    entry: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model: str,
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", model)
    payload = {
        "verdict": "FAIL",
        "trials": [
            {
                "suite": "same-fact",
                "trial": 1,
                "completion": "timeout",
                "cases": {"same-fact-001": False},
            }
        ],
    }
    run = Mock(return_value=payload)
    monkeypatch.setattr(entry, "run_quality_trials", run)
    with pytest.raises(AssertionError):
        entry.test_extraction_quality()
    saved = json.loads((tmp_path / f"extraction-quality-{model}.json").read_text())
    assert saved["model"] == model
    assert saved["verdict"] == "FAIL"
    assert saved["trials"] == payload["trials"]
    run.assert_called_once()


def test_existing_result_is_rejected_before_ingesting(
    entry: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", "gemma4-12b")
    path = tmp_path / "extraction-quality-gemma4-12b.json"
    path.write_text('{"original": true}')
    run = Mock()
    monkeypatch.setattr(entry, "run_quality_trials", run)
    with pytest.raises((FileExistsError, ValueError)):
        entry.test_extraction_quality()
    run.assert_not_called()
    assert path.read_text() == '{"original": true}'


def test_sdk_client_is_closed_when_run_fails_before_results(
    entry: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", "gemma4-12b")
    client = MagicMock()
    client.aclose = AsyncMock()
    monkeypatch.setattr(entry, "_open_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        entry, "run_quality_trials", Mock(side_effect=ValueError("synthetic-invalid-input"))
    )
    with pytest.raises(ValueError):
        entry.test_extraction_quality()
    client.aclose.assert_awaited_once()
    assert not list(tmp_path.glob("*.json"))


def test_existing_progress_is_rejected_before_ingesting(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", "gemma4-12b")
    path = tmp_path / "extraction-quality-gemma4-12b.progress.jsonl"
    path.write_text('{"original": true}\n')
    run = Mock()
    monkeypatch.setattr(entry, "run_quality_trials", run)
    with pytest.raises(FileExistsError):
        entry.test_extraction_quality()
    run.assert_not_called()
    assert path.read_text() == '{"original": true}\n'
    assert not (tmp_path / "extraction-quality-gemma4-12b.json").exists()


@pytest.mark.parametrize("stop_at", [1, 4, None])
def test_completed_trials_remain_readable_when_execution_is_interrupted(
    entry: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stop_at: int | None,
) -> None:
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", "gemma4-12b")
    progress = tmp_path / "extraction-quality-gemma4-12b.progress.jsonl"
    final = tmp_path / "extraction-quality-gemma4-12b.json"

    class Interrupted(BaseException):
        pass

    class Ports:
        def __init__(self) -> None:
            self.wait_count = 0
            self.ingest_count = 0

        def ingest(self, plan: IngestPlan) -> None:
            # Read through a separate handle before the next trial starts.
            with progress.open(encoding="utf-8") as source:
                assert len(list(source)) == self.ingest_count
            self.ingest_count += 1

        def wait(self, bank: str) -> CompletionResult:
            self.wait_count += 1
            if self.wait_count == stop_at:
                raise Interrupted()
            return CompletionResult("timeout", "completion wait limit exceeded")

        def read(self, bank: str) -> object:
            pytest.fail("Unfinished trials must not read memories")

        def recall(self, character: str, request: object) -> object:
            pytest.fail("Unfinished trials must not recall")

    ports = Ports()
    client = MagicMock()
    client.aclose = AsyncMock()
    monkeypatch.setattr(entry, "_open_client", AsyncMock(return_value=client))
    monkeypatch.setattr(entry, "HttpIngestPort", lambda http: ports)
    monkeypatch.setattr(entry, "HindsightQualityPort", lambda client, runner, database: ports)
    with pytest.raises(AssertionError if stop_at is None else Interrupted):
        entry.test_extraction_quality()
    rows = [json.loads(line) for line in progress.read_text().split("\n") if line]
    assert len(rows) == (9 if stop_at is None else stop_at - 1)
    assert all(row["completion"] == "timeout" for row in rows)
    assert all(not any(row["cases"].values()) and row["metrics"] is None for row in rows)
    assert ports.wait_count == (9 if stop_at is None else stop_at)
    client.aclose.assert_awaited_once()
    if stop_at is None:
        saved = json.loads(final.read_text())
        assert saved["trials"] == rows
        assert saved["verdict"] == "FAIL"
        assert set(saved) == {"model", "scorer_version", "trials", "verdict"}
    else:
        assert not final.exists()
        assert all(row["trial"] == 1 for row in rows)
