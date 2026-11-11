"""Consistency runner evidence and transparent component recording with synthetic ports."""

import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.consistency.ports import ConsistencyReview, ReviewFailed
from digital_souls_memory_hindsight.consistency.ports import Reviewer as ReviewerPort
from digital_souls_memory_hindsight.identity.ports import (
    IdentityUnavailable,
    LLMGateway,
    Temperament,
)
from digital_souls_memory_hindsight.voice.domain import VoiceReview
from tests.consistency_doubles import Gateway, Reviewer
from tests.it2.quality_run import JsonlTrialRecorder
from tests.it2.scoring import SCORER_VERSION
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import MemoryStore
from tests.ut.test_identity_service import IdentityStore
from tests.ut.test_voice_http import DeletionState, VoiceStore

pytestmark = pytest.mark.ut


@pytest.fixture
def runner() -> ModuleType:
    return import_module("tests.it2.consistency_run")


@pytest.mark.parametrize("operation", ["personality", "voice"])
def test_recorded_generation_preserves_inputs_and_returned_text(
    runner: ModuleType, operation: str
) -> None:
    delegate = Mock(spec=Gateway)
    delegate.render_personality.return_value = "合成の人格"
    delegate.respond_in_voice.return_value = "合成の返答"
    gateway = runner.RecordingGateway(delegate)
    temperament = Temperament("synthetic", 1, "穏やか", datetime(2026, 11, 5, tzinfo=UTC))
    if operation == "personality":
        result = gateway.render_personality({"Compassion": 0.9}, temperament, "synthetic-v1")
        delegate.render_personality.assert_called_once_with(
            {"Compassion": 0.9}, temperament, "synthetic-v1"
        )
        assert result == "合成の人格"
    else:
        result = gateway.respond_in_voice("初対面", "よろしく", None, "穏やか")
        delegate.respond_in_voice.assert_called_once_with("初対面", "よろしく", None, "穏やか")
        assert result == "合成の返答"
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["output"] == result
    assert gateway.calls[0]["error"] is None


@pytest.mark.parametrize("operation", ["personality", "voice"])
def test_recorded_generation_rethrows_and_records_type_without_exception_body(
    runner: ModuleType, operation: str
) -> None:
    delegate = Mock(spec=Gateway)
    delegate.render_personality.side_effect = IdentityUnavailable("synthetic-private-detail")
    delegate.respond_in_voice.side_effect = IdentityUnavailable("synthetic-private-detail")
    gateway = runner.RecordingGateway(delegate)
    temperament = Temperament("synthetic", 1, "穏やか", datetime(2026, 11, 5, tzinfo=UTC))
    with pytest.raises(IdentityUnavailable):
        if operation == "personality":
            gateway.render_personality({"Compassion": 0.9}, temperament, "synthetic-v1")
        else:
            gateway.respond_in_voice("初対面", "よろしく", None, "穏やか")
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["error"] == "IdentityUnavailable"
    assert "synthetic-private-detail" not in json.dumps(gateway.calls, ensure_ascii=False)


@pytest.mark.parametrize("consistent", [True, False])
@pytest.mark.parametrize("operation", ["personality", "voice"])
def test_recorded_reviewer_preserves_arguments_decision_and_reason(
    runner: ModuleType, consistent: bool, operation: str
) -> None:
    delegate = Reviewer(consistent=consistent, reason="合成の審査理由")
    reviewer = runner.RecordingReviewer(delegate)
    if operation == "personality":
        result = reviewer.review_personality("穏やか", "常に穏やかに応対する")
        assert delegate.personality_calls == [("穏やか", "常に穏やかに応対する")]
    else:
        result = reviewer.review_voice("初対面", "よろしく", "よろしく", None, "穏やか")
        assert delegate.voice_calls == [("初対面", "よろしく", "よろしく", None, "穏やか")]
    assert result == ConsistencyReview(consistent, "合成の審査理由")
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0]["review"] == {"consistent": consistent, "reason": "合成の審査理由"}
    assert reviewer.calls[0]["error"] is None


def test_recorded_review_failure_keeps_type_only_and_does_not_retry(runner: ModuleType) -> None:
    delegate = Reviewer(error=ReviewFailed("synthetic-private-detail"))
    reviewer = runner.RecordingReviewer(delegate)
    with pytest.raises(ReviewFailed):
        reviewer.review_personality("穏やか", "合成の人格")
    assert len(delegate.personality_calls) == 1
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0]["error"] == "ReviewFailed"
    assert "synthetic-private-detail" not in json.dumps(reviewer.calls, ensure_ascii=False)


@pytest.mark.parametrize("operation", ["personality", "voice"])
@pytest.mark.parametrize("consistent", [True, False])
def test_reviewer_cases_send_fixed_text_directly_without_generation(
    runner: ModuleType, operation: str, consistent: bool
) -> None:
    delegate = Reviewer(consistent=consistent)
    reviewer = runner.RecordingReviewer(delegate)
    inputs: dict[str, str | None] = (
        {"temperament": "常に穏やか", "personality": "決して怒らない"}
        if operation == "personality"
        else {
            "situation": "初対面",
            "response": "よろしくお願いします",
            "answer": "よろしくお願いします",
            "question": None,
            "temperament": "丁寧",
        }
    )
    case = {
        "id": "reviewer-synthetic-001",
        "level": "reviewer",
        "operation": operation,
        "input": inputs,
        "expected": {"consistent": consistent},
    }
    result = runner.run_reviewer_case(case=case, reviewer=reviewer)
    assert result["outcome"] == "correct"
    if operation == "personality":
        assert delegate.personality_calls == [(inputs["temperament"], inputs["personality"])]
        assert delegate.voice_calls == []
    else:
        assert delegate.voice_calls == [
            tuple(
                inputs[key]
                for key in ("situation", "response", "answer", "question", "temperament")
            )
        ]
        assert delegate.personality_calls == []
    assert all(step["generation_calls"] == 0 for step in result["steps"])


def trial_cases() -> list[dict[str, object]]:
    return [
        {
            "id": "reviewer-synthetic-001",
            "level": "reviewer",
            "operation": "personality",
            "input": {"temperament": "穏やか", "personality": "決して怒らない"},
            "expected": {"consistent": True},
        },
        {
            "id": "path-synthetic-001",
            "level": "path",
            "operation": "seed",
            "input": {},
            "expected": {"consistent": True},
        },
    ]


@pytest.mark.parametrize("first", ["correct", "generation_failed", "exception"])
def test_runner_executes_exactly_three_trials_and_records_every_case_without_retry(
    runner: ModuleType, tmp_path: Path, first: str
) -> None:
    cases = trial_cases()
    calls: list[tuple[str, str]] = []
    path = tmp_path / "progress.jsonl"
    recorder = JsonlTrialRecorder(path)

    class Executor:
        def execute(self, *, case: dict[str, object], character_id: str) -> dict[str, object]:
            calls.append((str(case["id"]), character_id))
            if len(calls) == 1 and first == "exception":
                raise RuntimeError("synthetic-private-detail")
            return {
                "id": case["id"],
                "level": case["level"],
                "character_id": character_id,
                "outcome": first if len(calls) == 1 else "correct",
                "reason": "合成の実行結果",
                "steps": [],
            }

    result = runner.run_consistency_trials(cases=cases, executor=Executor(), recorder=recorder)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert result["trials"] == rows
    assert [row["trial"] for row in rows] == [1, 2, 3]
    assert len(calls) == 6
    assert len({character for _, character in calls}) == 6
    assert [identifier for identifier, _ in calls] == [case["id"] for case in cases] * 3
    assert all(len(row["cases"]) == 2 for row in rows)
    assert result["verdict"] == ("PASS" if first == "correct" else "FAIL")
    failed = rows[0]["cases"][0]
    assert failed["outcome"] == ("execution_error" if first == "exception" else first)
    if first == "exception":
        assert "RuntimeError" in failed["reason"]
        assert "synthetic-private-detail" not in json.dumps(rows, ensure_ascii=False)


def test_completed_trial_is_flushed_before_interruption_of_next_trial(
    runner: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "progress.jsonl"
    recorder = JsonlTrialRecorder(path)
    count = 0

    class Interrupted(BaseException):
        pass

    class Executor:
        def execute(self, *, case: dict[str, object], character_id: str) -> dict[str, object]:
            nonlocal count
            count += 1
            if count == 3:
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                assert len(rows) == 1
                assert rows[0]["trial"] == 1
                assert len(rows[0]["cases"]) == 2
                raise Interrupted()
            return {
                "id": case["id"],
                "level": case["level"],
                "character_id": character_id,
                "outcome": "correct",
                "reason": "合成結果",
                "steps": [],
            }

    with pytest.raises(Interrupted):
        runner.run_consistency_trials(cases=trial_cases(), executor=Executor(), recorder=recorder)
    assert count == 3
    assert len(path.read_text().splitlines()) == 1


def test_recording_failure_stops_execution_before_next_trial(runner: ModuleType) -> None:
    executor = Mock()
    executor.execute.return_value = {
        "id": "reviewer-synthetic-001",
        "level": "reviewer",
        "outcome": "correct",
        "reason": "合成結果",
        "steps": [],
    }
    recorder = Mock()
    recorder.record.side_effect = OSError("synthetic-write-failure")
    with pytest.raises(OSError):
        runner.run_consistency_trials(cases=trial_cases()[:1], executor=executor, recorder=recorder)
    assert executor.execute.call_count == 1


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic@postgres/ledger")
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://llama-generation:8080/v1")
    monkeypatch.setenv("MEMORY_LLM_MODEL", "synthetic-review-model")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "synthetic-test-only")
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("IT2_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("IT2_GENERATION_MODEL", "synthetic-other-model")
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    return import_module("tests.it2.test_consistency")


def test_supervisor_entry_is_marked_only_it2(entry: ModuleType) -> None:
    marks = entry.pytestmark
    if not isinstance(marks, list):
        marks = [marks]
    assert {mark.name for mark in marks} == {"it2"}


def test_supervisor_entry_import_does_not_require_runtime_configuration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-c", "import tests.it2.test_consistency"],
        cwd=root,
        env={"HOME": str(tmp_path), "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_failed_entry_saves_all_trials_using_component_model_before_asserting(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {
        "verdict": "FAIL",
        "levels": {"reviewer": "PASS", "path": "FAIL"},
        "trials": [{"trial": trial, "cases": []} for trial in (1, 2, 3)],
    }
    run = Mock(return_value=payload)
    monkeypatch.setattr(entry, "run_consistency_trials", run)
    with pytest.raises(AssertionError):
        entry.test_consistency(monkeypatch)
    saved = json.loads((tmp_path / "consistency-synthetic-review-model.json").read_text())
    assert saved["model"] == "synthetic-review-model"
    assert saved["scorer_version"] == SCORER_VERSION
    assert saved["verdict"] == "FAIL"
    assert saved["trials"] == payload["trials"]
    assert saved["levels"] == payload["levels"]
    run.assert_called_once()


@pytest.mark.parametrize("suffix", [".json", ".progress.jsonl"])
def test_entry_rejects_existing_evidence_before_running(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    target = tmp_path / f"consistency-synthetic-review-model{suffix}"
    target.write_text('{"original": true}\n', encoding="utf-8")
    run = Mock()
    monkeypatch.setattr(entry, "run_consistency_trials", run)
    with pytest.raises(FileExistsError):
        entry.test_consistency(monkeypatch)
    run.assert_not_called()
    assert json.loads(target.read_text()) == {"original": True}


def test_missing_component_configuration_is_failure_not_skip(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in (
        "MEMORY_LLM_BASE_URL",
        "MEMORY_LLM_MODEL",
        "MEMORY_LLM_API_KEY",
        "MEMORY_LLM_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    run = Mock()
    monkeypatch.setattr(entry, "run_consistency_trials", run)
    with pytest.raises(ValueError):
        entry.test_consistency(monkeypatch)
    run.assert_not_called()
    assert not list(tmp_path.glob("consistency-*"))


@pytest.mark.parametrize("model", ["../unsafe", "name/unsafe", " \t "])
def test_invalid_model_filename_is_rejected_before_running(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str
) -> None:
    monkeypatch.setenv("MEMORY_LLM_MODEL", model)
    run = Mock()
    monkeypatch.setattr(entry, "run_consistency_trials", run)
    with pytest.raises(ValueError):
        entry.test_consistency(monkeypatch)
    run.assert_not_called()
    assert not list(tmp_path.glob("consistency-*"))


class SyntheticApplications:
    """Real registration routes with stores that survive creation of a new app."""

    def __init__(self, character: str) -> None:
        self.character = character
        self.identity = IdentityStore()
        self.voices = VoiceStore()
        self.evidence = MemoryStore()
        self.apps = 0
        self.deleted: list[str] = []

    @contextmanager
    def open(self, gateway: LLMGateway, reviewer: ReviewerPort) -> Iterator[TestClient]:
        from digital_souls_memory_hindsight.evidence.service import EvidenceService
        from digital_souls_memory_hindsight.evidence_http import evidence_router
        from digital_souls_memory_hindsight.identity.service import IdentityService
        from digital_souls_memory_hindsight.identity_http import identity_router
        from digital_souls_memory_hindsight.voice.service import VoiceService
        from digital_souls_memory_hindsight.voice_http import voice_router

        self.apps += 1
        ledger = EvidenceService(self.evidence, frozenset({self.character}), clock=Clock())
        identity = IdentityService(self.identity, gateway, reviewer, clock=Clock())
        voice = VoiceService(
            self.voices, ledger, DeletionState(), self.identity, gateway, reviewer, clock=Clock()
        )
        app = FastAPI()
        app.include_router(evidence_router(lambda: ledger))
        app.include_router(identity_router(lambda: identity))
        app.include_router(voice_router(lambda: voice))
        with TestClient(app) as client:
            yield client

    def review_row(self, character_id: str) -> dict[str, object] | None:
        row = self.voices.reviews.get(character_id)
        return (
            None
            if row is None
            else {
                "character_id": character_id,
                "consistent": row.result.consistent,
                "reason": row.result.reason,
                "reviewed_at": row.reviewed_at.isoformat(),
            }
        )

    def delete_source(self, client: TestClient, turn_id: str) -> None:
        self.deleted.append(turn_id)
        assert turn_id in self.evidence.turns
        self.evidence.turns.pop(turn_id)
        self.evidence.tombstones.add(turn_id)
        self.voices.values = {
            key: value
            for key, value in self.voices.values.items()
            if value.answer_span is None or value.answer_span.turn_id != turn_id
        }


@pytest.mark.parametrize("operation", ["seed", "temperament", "voice_first"])
@pytest.mark.parametrize("consistent", [True, False])
def test_http_scenarios_observe_real_route_decisions_and_saved_versions(
    runner: ModuleType, operation: str, consistent: bool
) -> None:
    scoring = import_module("tests.it2.consistency")
    corpus = Path(__file__).resolve().parents[1] / "it2/cases/consistency.json"
    case = next(
        c
        for c in scoring.load_consistency_cases(corpus)
        if c["operation"] == operation and c["expected"]["consistent"] == consistent
    )
    applications = SyntheticApplications("synthetic-http")
    gateway = Mock(spec=Gateway)
    gateway.render_personality.return_value = "合成の人格"
    gateway.respond_in_voice.return_value = "合成の返答"
    reviewer = Mock(spec=Reviewer)
    target = ConsistencyReview(consistent, "合成の判断理由")
    reviewer.review_personality.side_effect = (
        [ConsistencyReview(True, "準備の審査"), target] if operation == "temperament" else [target]
    )
    reviewer.review_voice.return_value = target
    executor = runner.HttpConsistencyExecutor(gateway, reviewer, applications, applications)
    result = executor.execute(case=case, character_id="synthetic-http")
    assert result["outcome"] == "correct", result
    steps = result["steps"]
    assert steps[0]["response_review"] is None
    final = steps[-1]
    assert final["status"] == (422 if not consistent and operation != "voice_first" else 201)
    assert final["response_review"]["consistent"] is consistent
    assert final["saved"] is (consistent or operation == "voice_first")
    if operation == "temperament":
        assert final["candidate_version_status"] == (200 if consistent else 404)
        if not consistent:
            assert final["current_before"] == final["current_after"]
    assert applications.apps == 1


@pytest.mark.parametrize("status", [201, 404])
def test_voice_first_without_generation_is_a_contract_violation(
    runner: ModuleType, status: int
) -> None:
    scoring = import_module("tests.it2.consistency")
    corpus = Path(__file__).resolve().parents[1] / "it2/cases/consistency.json"
    case = next(
        c
        for c in scoring.load_consistency_cases(corpus)
        if c["operation"] == "voice_first" and c["expected"]["consistent"] is True
    )
    applications = SyntheticApplications("synthetic-http" if status == 201 else "other-character")
    if status == 201:
        applications.voices.reviews["synthetic-http"] = VoiceReview(
            ConsistencyReview(True, "合成の審査理由"), datetime(2026, 11, 5, tzinfo=UTC)
        )
    gateway, reviewer = Gateway(), Reviewer()
    result = runner.HttpConsistencyExecutor(gateway, reviewer, applications, applications).execute(
        case=case, character_id="synthetic-http"
    )
    final = result["steps"][-1]
    assert final["stage"] == "first"
    assert final["status"] == status
    assert final["saved"] is (status == 201)
    assert final["generation_calls"] == final["review_calls"] == 0
    assert final["generated_text"] is None
    assert final["generation_error"] is final["review_error"] is None
    assert final["review"] is final["response_review"] is None
    assert gateway.voice_calls == []
    assert reviewer.voice_calls == []
    assert result["outcome"] == final["outcome"] == "contract_violation"


@pytest.mark.parametrize("failure", ["generation", "review", "preparation"])
def test_http_scenarios_preserve_failure_stage_instead_of_classifying_only_503(
    runner: ModuleType, failure: str
) -> None:
    scoring = import_module("tests.it2.consistency")
    corpus = Path(__file__).resolve().parents[1] / "it2/cases/consistency.json"
    case = next(
        c
        for c in scoring.load_consistency_cases(corpus)
        if c["operation"] == ("temperament" if failure == "preparation" else "seed")
        and c["expected"]["consistent"] is True
    )
    applications = SyntheticApplications("synthetic-http")
    gateway = Mock(spec=Gateway)
    gateway.render_personality.return_value = "合成の人格"
    reviewer = Mock(spec=Reviewer)
    reviewer.review_personality.return_value = ConsistencyReview(True, "合成の理由")
    if failure in {"generation", "preparation"}:
        gateway.render_personality.side_effect = IdentityUnavailable("synthetic-private-detail")
    else:
        reviewer.review_personality.side_effect = ReviewFailed("synthetic-private-detail")
    result = runner.HttpConsistencyExecutor(gateway, reviewer, applications, applications).execute(
        case=case, character_id="synthetic-http"
    )
    assert result["outcome"] == ("review_failed" if failure == "review" else "generation_failed")
    assert result["steps"][-1]["status"] == 503
    assert result["steps"][-1]["saved"] is False
    assert result["steps"][-1]["stage"] == "author_seed"
    assert len(result["steps"]) == 2
    assert "synthetic-private-detail" not in json.dumps(result, ensure_ascii=False)


def test_voice_scenario_reuses_character_review_after_source_deletion_and_app_recreation(
    runner: ModuleType,
) -> None:
    scoring = import_module("tests.it2.consistency")
    corpus = Path(__file__).resolve().parents[1] / "it2/cases/consistency.json"
    case = next(
        c for c in scoring.load_consistency_cases(corpus) if c["operation"] == "voice_no_rereview"
    )
    applications = SyntheticApplications("synthetic-lifetime")
    gateway, reviewer = Gateway(), Reviewer()
    result = runner.HttpConsistencyExecutor(gateway, reviewer, applications, applications).execute(
        case=case, character_id="synthetic-lifetime"
    )
    assert result["outcome"] == "correct", result
    assert applications.apps == 2
    assert applications.deleted == ["synthetic-lifetime-turn"]
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1
    steps = result["steps"]
    assert [step["stage"] for step in steps] == [
        "initial_temperament",
        "first_turn",
        "after_source_deletion",
        "after_deletion",
        "subsequent",
        "after_restart",
    ]
    first_review = steps[1]["response_review"]
    assert first_review["consistent"] is True
    for step in steps[2:]:
        assert step["response_review"] is None
        assert step["generation_calls"] == step["review_calls"] == 0
        assert step["review_row_before"] == step["review_row_after"]
    assert len(applications.voices.values) == 3


@pytest.mark.parametrize("change", ["deletion", "restart"])
def test_voice_scenario_rejects_loss_of_review_across_lifecycle_changes(
    runner: ModuleType, change: str
) -> None:
    scoring = import_module("tests.it2.consistency")
    corpus = Path(__file__).resolve().parents[1] / "it2/cases/consistency.json"
    case = next(
        c for c in scoring.load_consistency_cases(corpus) if c["operation"] == "voice_no_rereview"
    )

    class ChangedApplications(SyntheticApplications):
        def delete_source(self, client: TestClient, turn_id: str) -> None:
            super().delete_source(client, turn_id)
            if change == "deletion":
                self.voices.reviews.clear()

        @contextmanager
        def open(self, gateway: LLMGateway, reviewer: ReviewerPort) -> Iterator[TestClient]:
            if change == "restart" and self.apps == 1:
                self.voices.reviews.clear()
            with super().open(gateway, reviewer) as client:
                yield client

    applications = ChangedApplications("synthetic-lifetime")
    gateway, reviewer = Gateway(), Reviewer()
    result = runner.HttpConsistencyExecutor(gateway, reviewer, applications, applications).execute(
        case=case, character_id="synthetic-lifetime"
    )
    assert result["outcome"] == "contract_violation"
    failed = next(step for step in result["steps"] if step["outcome"] != "correct")
    assert failed["stage"] == ("after_source_deletion" if change == "deletion" else "after_restart")
    if change == "deletion":
        assert failed["review_row_after"] is None
        assert len(gateway.voice_calls) == 1
    else:
        assert failed["generation_calls"] == failed["review_calls"] == 1
        assert len(gateway.voice_calls) == 2


@pytest.mark.parametrize("exists", [True, False])
def test_review_storage_reads_only_the_selected_characters_persisted_row(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    stamp = datetime(2026, 11, 5, tzinfo=UTC)
    row = {
        "character_id": "synthetic",
        "exemplar_id": "first",
        "consistent": False,
        "reason": "合成の審査",
        "reviewed_at": stamp,
    }
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = row if exists else None
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(runner.psycopg, "connect", connect)
    result = runner.PostgresConsistencyState("synthetic-database", "synthetic-only").review_row(
        "synthetic"
    )
    assert result == ({**row, "reviewed_at": stamp.isoformat()} if exists else None)
    query, parameters = connection.execute.call_args.args
    assert query.lstrip().startswith("SELECT ")
    assert parameters == ("synthetic",)
    assert connection.execute.call_count == 1


@pytest.mark.parametrize("status", ["completed", "failed", "processing"])
def test_source_deletion_requires_completed_status_within_deadline(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    client = Mock(spec=TestClient)
    client.post.return_value = httpx.Response(202, json={"status": "accepted"})
    client.get.return_value = httpx.Response(200, json={"status": status})
    clock = Mock(side_effect=[0.0, 0.0, 1801.0] if status == "processing" else [0.0, 0.0, 0.0])
    monkeypatch.setattr(runner.time, "monotonic", clock)
    state = runner.PostgresConsistencyState("synthetic-database", "synthetic-only")
    if status == "completed":
        state.delete_source(client, "synthetic-turn")
    else:
        with pytest.raises(TimeoutError if status == "processing" else ValueError):
            state.delete_source(client, "synthetic-turn")
    assert client.post.call_count == client.get.call_count == 1
    assert client.post.call_args.kwargs["json"] == {
        "request_id": "synthetic-turn-delete",
        "target_turn_ids": ["synthetic-turn"],
    }
    assert client.get.call_args.args == ("/deletion-requests/synthetic-turn-delete",)


def test_deletion_success_received_after_deadline_is_not_accepted(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Mock(spec=TestClient)
    client.post.return_value = httpx.Response(202, json={})
    client.get.return_value = httpx.Response(200, json={"status": "completed"})
    monkeypatch.setattr(runner.time, "monotonic", Mock(side_effect=[0.0, 0.0, 1801.0]))
    with pytest.raises(TimeoutError):
        runner.PostgresConsistencyState("synthetic-database", "synthetic-only").delete_source(
            client, "synthetic-turn"
        )


def test_app_factory_passes_recording_components_to_a_new_http_app(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("synthetic_app")
    app = FastAPI()
    app.get("/health")(lambda: {"status": "ok"})
    create = Mock(return_value=app)
    monkeypatch.setattr(module, "create_app", create, raising=False)
    monkeypatch.setattr(module, "Worker", Mock(), raising=False)
    monkeypatch.setattr(entry.importlib, "import_module", Mock(return_value=module))
    gateway, reviewer = Gateway(), Reviewer()
    with entry.ApplicationClients(monkeypatch).open(gateway, reviewer) as client:
        assert client.get("/health").json() == {"status": "ok"}
    create.assert_called_once_with(llm_gateway=gateway, reviewer=reviewer)
