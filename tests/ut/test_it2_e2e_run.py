"""Supervisor entry contracts; unit tests never execute real-model trials."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import MagicMock, Mock

import httpx
import pytest

from digital_souls_memory_hindsight.evidence.projection import bank_id, document_id
from digital_souls_memory_hindsight.evidence_http import SessionRequest, TurnRequest
from tests.it2.completion import CompletionResult
from tests.it2.e2e import STEPS, load_e2e_case
from tests.it2.e2e_run import E2ETrial, PostgresE2EState, run_e2e_trial, run_e2e_trials
from tests.it2.quality_contract import MemoryRow
from tests.it2.quality_run import JsonlTrialRecorder
from tests.it2.scoring import SCORER_VERSION

pytestmark = pytest.mark.ut


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key, value in {
        "DATABASE_URL": "postgresql://synthetic@postgres/ledger",
        "SERVICE_URL": "http://service:8000",
        "HINDSIGHT_URL": "http://hindsight:8888",
        "MEMORY_AUTHOR_ID": "synthetic-author",
        "MEMORY_AUTHOR_TOKEN": "synthetic-e2e-author-only",
        "MEMORY_LLM_BASE_URL": "http://llama-generation:8080/v1",
        "MEMORY_LLM_MODEL": "synthetic-component-model",
        "MEMORY_LLM_API_KEY": "synthetic-only",
        "MEMORY_LLM_TIMEOUT_SECONDS": "10",
        "IT2_GENERATION_MODEL": "synthetic-generation-model",
        "IT2_RESULT_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    return import_module("tests.it2.test_e2e")


def test_e2e_entry_imports_without_environment_configuration(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import tests.it2.test_e2e"],
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_e2e_entry_is_only_marked_it2(entry: ModuleType) -> None:
    marks = entry.pytestmark
    if not isinstance(marks, list):
        marks = [marks]
    assert {mark.name for mark in marks} == {"it2"}


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_all_trials_are_saved_before_verdict_is_asserted(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict: str
) -> None:
    trials = [{"trial": trial, "reason": "合成の結果"} for trial in (1, 2, 3)]
    run = Mock(return_value={"verdict": verdict, "trials": trials})
    monkeypatch.setattr(entry, "run_e2e_trials", run)
    if verdict == "FAIL":
        with pytest.raises(AssertionError):
            entry.test_e2e()
    else:
        entry.test_e2e()
    saved = json.loads((tmp_path / "e2e-synthetic-generation-model.json").read_text())
    assert saved["generation_model"] == "synthetic-generation-model"
    assert saved["memory_llm_model"] == "synthetic-component-model"
    assert saved["scorer_version"] == SCORER_VERSION
    assert saved["verdict"] == verdict
    assert saved["trials"] == trials
    run.assert_called_once()


@pytest.mark.parametrize("suffix", [".json", ".progress.jsonl"])
def test_existing_result_or_progress_is_rejected_before_running(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    path = tmp_path / ("e2e-synthetic-generation-model" + suffix)
    original = '{"synthetic_original": true}\n'
    path.write_text(original)
    run = Mock()
    monkeypatch.setattr(entry, "run_e2e_trials", run)
    with pytest.raises(FileExistsError):
        entry.test_e2e()
    run.assert_not_called()
    assert path.read_text() == original


@pytest.mark.parametrize("model", ["../synthetic", "synthetic/model", ""])
def test_invalid_model_filename_is_rejected_before_running(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str
) -> None:
    monkeypatch.setenv("IT2_GENERATION_MODEL", model)
    run = Mock()
    monkeypatch.setattr(entry, "run_e2e_trials", run)
    with pytest.raises(ValueError):
        entry.test_e2e()
    run.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "missing", ["MEMORY_AUTHOR_TOKEN", "IT2_GENERATION_MODEL", "IT2_RESULT_DIR"]
)
def test_missing_required_configuration_fails_before_running(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    run = Mock()
    monkeypatch.setattr(entry, "run_e2e_trials", run)
    with pytest.raises((KeyError, ValueError)):
        entry.test_e2e()
    run.assert_not_called()


def test_unsafe_report_is_rejected_before_final_file_is_written(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = Mock(
        return_value={
            "verdict": "FAIL",
            "trials": [],
            "Authorization": "Bearer synthetic-private-token",
        }
    )
    monkeypatch.setattr(entry, "run_e2e_trials", run)
    with pytest.raises(ValueError):
        entry.test_e2e()
    assert not (tmp_path / "e2e-synthetic-generation-model.json").exists()


class SyntheticService:
    """HTTP/inspection double; it does not replace any scenario operation."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, object]] = {}
        self.turns: dict[str, dict[str, object]] = {}
        self.identities: dict[str, dict[str, dict[str, object]]] = {}
        self.voices: dict[str, list[dict[str, object]]] = {}
        self.profiles: dict[str, dict[str, object]] = {}
        self.saved: list[dict[str, object]] = []
        self.state_rows: dict[tuple[str, str, str], dict[str, object]] = {}
        self.outbox = 0
        self.waits = 0
        self.completion = "complete"
        self.deleting = False
        self.deleted: set[str] = set()
        self.bad_state = False
        self.pending_state = False
        self.reject_session = False
        self.stale_core = False
        self.stale_content = False
        self.corrupt_turn = False
        self.receipts: dict[str, list[dict[str, object]]] = {}
        self.deletion_status = "completed"
        self.error: Exception | None = None
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wait(self, bank: str) -> CompletionResult:
        self.waits += 1
        return CompletionResult(self.completion, "synthetic completion")  # type: ignore[arg-type]

    def read(self, bank: str) -> list[MemoryRow]:
        main = next(
            s
            for s in self.sessions
            if s.endswith("-session-main") and bank_id(s.removesuffix("-session-main")) == bank
        )
        return (
            []
            if any(t in self.deleted for t in self.turns if self.turns[t]["session_id"] == main)
            else [
                {
                    "id": "synthetic-memory",
                    "document_id": document_id(main),
                    "text": "合成星砂祭",
                    "source_memory_ids": [],
                },
            ]
        )

    def provisions(self, character: str) -> list[dict[str, object]]:
        return deepcopy([r for r in self.saved if r["character_id"] == character])

    def states(self, character: str) -> list[dict[str, object]]:
        return deepcopy([r for (c, _, _), r in self.state_rows.items() if c == character])

    def outbox_count(self, bank: str) -> int:
        return self.outbox

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        body = cast(dict[str, object], json.loads(request.content) if request.content else {})
        parts = request.url.path.strip("/").split("/")
        method = request.method
        if parts[0] == "sessions":
            if method == "POST":
                if self.reject_session:
                    return httpx.Response(503, json={"detail": "Bearer synthetic-private-token"})
                body = SessionRequest.model_validate(body).model_dump(mode="json")
                self.sessions[cast(str, body["session_id"])] = body
                return httpx.Response(201, json=body)
            return httpx.Response(200, json=self.sessions[parts[1]])
        if parts[0] == "turns":
            if method == "POST" and len(parts) == 1:
                body = TurnRequest.model_validate(body).model_dump(mode="json", by_alias=True)
                body["recorded_at"] = "2026-11-05T00:01:01Z"
                if self.corrupt_turn and cast(str, body["turn_id"]).endswith("-turn-main"):
                    body["text"] = "別の原文"
                self.turns[cast(str, body["turn_id"])] = body
                self.outbox += 1
                return httpx.Response(201, json=body)
            turn = self.turns[parts[1]]
            if parts[1] in self.deleted:
                return httpx.Response(404, json={})
            if len(parts) == 2:
                return httpx.Response(200, json=turn)
            if method == "GET":
                return httpx.Response(200, json=self.receipts.get(parts[1], []))
            self.receipts.setdefault(parts[1], []).append({**body, "turn_id": parts[1]})
            if parts[1].endswith("-turn-reply") and self.waits == 0:
                return httpx.Response(201, json=body)
            assert self.waits >= 2, "Judged generation must follow both completion waits"
            character = cast(list[str], turn["perceived_by"])[0]
            key = (character, cast(str, turn["session_id"]), cast(str, turn["surface"]))
            previous = self.state_rows.get(key)
            generation = 1 if previous is None else cast(int, previous["generation"]) + 1
            self.state_rows[key] = {
                "session_id": key[1],
                "surface": key[2],
                "generation": generation,
                "state_generation": None if self.pending_state else generation,
                "generated_at": None if self.pending_state else f"2026-11-05T00:00:0{generation}Z",
                "state": None
                if self.bad_state or self.pending_state
                else {
                    "mood": "穏やか",
                    "relationship_temperature": "親しい",
                    "current_topic": "合成の話題",
                },
                "status": "failed"
                if self.bad_state
                else "pending"
                if self.pending_state
                else "completed",
                "attempts": 3,
                "last_error": "gateway_unavailable" if self.bad_state else None,
            }
            return httpx.Response(201, json=body)
        if parts[0] == "deletion-requests":
            assert request.headers["Authorization"] == "Bearer synthetic-e2e-author-only"
            self.deleting = True
            if method == "POST":
                self.deleted.update(cast(list[str], body["target_turn_ids"]))
                self.saved = [
                    r
                    for r in self.saved
                    if not any(
                        s["turn_id"] in self.deleted
                        for s in cast(list[dict[str, object]], r["source_spans"])
                        + (
                            cast(list[dict[str, object]], r["profile_entries"])
                            if r["kind"] == "resident"
                            else []
                        )
                    )
                ]
                for (_, session, _), row in self.state_rows.items():
                    if session.endswith("-session-main"):
                        row["generation"] = cast(int, row["generation"]) + 1
                        row["state"] = None
                return httpx.Response(202, json={"status": "accepted"})
            return httpx.Response(200, json={"status": self.deletion_status})
        character, operation = parts[1], parts[2]
        if operation in {"core-identities", "temperaments", "personality-profiles"}:
            field = {
                "core-identities": "core_identity",
                "temperaments": "temperament",
                "personality-profiles": "personality_profile",
            }[operation]
            identity = self.identities.setdefault(character, {})
            version = cast(int, identity.get(field, {}).get("version", 0)) + 1
            value = {**body, "version": version}
            if field == "personality_profile":
                value = {"version": 1, "description": "架空の人格"}
            identity[field] = value
            return httpx.Response(201, json=value)
        if operation == "voice-exemplars":
            self.voices.setdefault(character, []).append(body)
            return httpx.Response(201, json=body)
        if operation == "profile":
            profile = self.profiles.get(character)
            self.profiles[character] = {
                **body,
                "revision": 1 if profile is None else cast(int, profile["revision"]) + 1,
            }
            return httpx.Response(200, json=self.profiles[character])
        if operation == "resident":
            profile = self.profiles[character]
            entries = [
                {
                    **s,
                    "text": cast(str, self.turns[cast(str, s["turn_id"])]["text"])[
                        cast(int, s["start"]) : cast(int, s["end"])
                    ],
                }
                for s in cast(list[dict[str, object]], profile["entries"])
                if s["turn_id"] not in self.deleted
            ]
            response: dict[str, object] = {
                "provision_id": f"{character}-resident-{len(self.saved)}",
                **deepcopy(self.identities[character]),
                "voice_exemplars": self.voices[character],
                "character_profile": {"revision": profile["revision"], "entries": entries},
            }
            if self.stale_core and character == "it2-e2e-t1":
                cast(dict[str, object], response["core_identity"])["version"] = 1
            if (
                self.stale_content
                and cast(dict[str, object], response["core_identity"])["version"] == 2
            ):
                cast(dict[str, object], response["core_identity"])["values"] = ["古い値"]
            from tests.ut.test_it2_e2e import resident_pair

            _, record = resident_pair()
            record.update(
                provision_id=response["provision_id"],
                character_id=character,
                session_id=body["session_id"],
                surface=body["surface"],
                core_identity_version=cast(dict[str, object], response["core_identity"])["version"],
                temperament_version=cast(dict[str, object], response["temperament"])["version"],
                personality_version=1,
                exemplar_ids=[v["exemplar_id"] for v in self.voices[character]],
                profile_revision=profile["revision"],
                profile_entries=[{k: s[k] for k in ("turn_id", "start", "end")} for s in entries],
                source_spans=[],
            )
            self.saved.append(record)
            return httpx.Response(200, json=response)
        if operation == "recall":
            assert self.waits >= 2
            if body.get("turn_id") in self.deleted:
                return httpx.Response(404, json={})
            from tests.ut.test_it2_e2e import turn_pair

            response, record, _ = turn_pair()
            source = next(t for t in self.turns.values() if t["session_id"] == body["session_id"])
            span = {"turn_id": source["turn_id"], "start": 0, "end": len(cast(str, source["text"]))}
            response.update(provision_id=f"{character}-recall-{len(self.saved)}")
            cast(list[dict[str, object]], response["memories"])[0]["source_turn_ids"] = [
                source["turn_id"]
            ]
            response["source_spans"] = [
                {
                    **cast(list[dict[str, object]], response["source_spans"])[0],
                    **span,
                    "text": source["text"],
                }
            ]
            response["verbatim_anchors"] = []
            recall_deleted = any(
                t in self.deleted
                for t in self.turns
                if self.turns[t]["session_id"] == body["session_id"]
            )
            if recall_deleted:
                response.update(memories=[], source_spans=[])
            record.update(
                provision_id=response["provision_id"],
                character_id=character,
                session_id=body["session_id"],
                resident_provision_id=body.get("resident_provision_id"),
                source_spans=[] if recall_deleted else [span],
                profile_entries=None,
                memory_ids=[] if recall_deleted else ["memory-2", "memory-1"],
                memory_trust=[] if recall_deleted else ["inferred", "inferred"],
                source_span_trust=[] if recall_deleted else ["evidence"],
                verbatim_anchor_ids=[],
                verbatim_anchor_trust=[],
            )
            self.saved.append(record)
            return httpx.Response(200, json=response)
        if operation == "sessions":
            state_row = self.state_rows.get((character, parts[3], request.url.params["surface"]))
            return httpx.Response(
                200, json={"state": None if state_row is None else state_row["state"]}
            )
        if operation == "turns":
            return httpx.Response(
                200, json=[t for t in self.turns.values() if t["turn_id"] not in self.deleted]
            )
        raise AssertionError(request.url.path)


def execute_synthetic(service: SyntheticService, character: str = "it2-e2e-t1") -> E2ETrial:
    with httpx.Client(
        transport=httpx.MockTransport(service.handle), base_url="http://synthetic"
    ) as http:
        return run_e2e_trial(
            case=load_e2e_case(Path("tests/it2/cases/e2e.json")),
            character_id=character,
            client=http,
            state=service,
            inspection=service,
            author_token="synthetic-e2e-author-only",
            clock=service.clock,
            sleep=service.sleep,
        )


def test_http_scenario_runs_all_seven_steps_and_preserves_first_resident_record() -> None:
    service = SyntheticService()
    result = execute_synthetic(service)
    assert result["cases"] == dict.fromkeys(STEPS, True), result
    requests = result["requests"]
    assert list(dict.fromkeys(r["step"] for r in requests)) == list(STEPS)
    observations = result["observations"]
    for step, version in (("resident_first", 1), ("resident_after_change", 2)):
        observed = cast(dict[str, object], observations[step])
        response = cast(dict[str, object], observed["response"])
        assert cast(dict[str, object], response["core_identity"])["version"] == version
    state = cast(dict[str, object], observations["character_state"])
    assert state["outbox_before"] == state["outbox_after"]


@pytest.mark.parametrize("completion", ["failed", "timeout"])
def test_unfinished_projection_stops_before_change_recall_and_judged_generation(
    completion: str,
) -> None:
    service = SyntheticService()
    service.completion = completion
    result = execute_synthetic(service)
    assert result["completion"] == completion
    assert result["cases"]["resident_first"] is True
    assert result["cases"]["resident_after_change"] is False
    assert result["failures"]["turn_recall"] == "not_reached"
    assert not any(r["path"].endswith("/recall") for r in result["requests"])


def test_state_generation_failure_is_recorded_and_deletion_still_runs() -> None:
    service = SyntheticService()
    service.bad_state = True
    result = execute_synthetic(service)
    assert result["cases"]["character_state"] is False
    assert "failed:gateway_unavailable" in result["failures"]["character_state"]
    assert result["cases"]["author_deletion"] is False
    assert service.deleting
    assert result["reason"] == result["failures"]["character_state"]


def test_changed_resident_mismatch_does_not_hide_later_steps_or_replace_first_reason() -> None:
    service = SyntheticService()
    service.stale_core = True
    result = execute_synthetic(service)
    assert result["cases"]["resident_after_change"] is False
    assert result["cases"]["turn_recall"] is True
    assert result["cases"]["author_deletion"] is True
    assert result["reason"] == result["failures"]["resident_after_change"]


def test_resident_with_new_version_but_stale_content_fails_change_step() -> None:
    service = SyntheticService()
    service.stale_content = True
    result = execute_synthetic(service)
    assert result["cases"]["resident_after_change"] is False


def test_turn_echo_and_get_cannot_both_substitute_different_input_text() -> None:
    service = SyntheticService()
    service.corrupt_turn = True
    result = execute_synthetic(service)
    assert result["cases"]["session_turn_receipt"] is False


def test_exception_records_only_type_and_keeps_all_unreached_steps() -> None:
    service = SyntheticService()
    service.error = RuntimeError("Bearer synthetic-private-token")
    result = execute_synthetic(service)
    assert result["reason"] == "execution_error:prerequisites:RuntimeError"
    assert not any(result["cases"].values())
    assert result["failures"]["author_deletion"] == "not_reached"


def test_unexpected_http_status_stops_trial_without_recording_response_body() -> None:
    service = SyntheticService()
    service.reject_session = True
    result = execute_synthetic(service)
    assert result["reason"] == "http_status:prerequisites:POST:/sessions:503"
    assert result["failures"]["session_turn_receipt"] == "not_reached"
    assert result["requests"][-1]["status"] == 503


def test_state_timeout_preserves_pending_job_and_still_attempts_deletion() -> None:
    service = SyntheticService()
    service.pending_state = True
    result = execute_synthetic(service)
    assert result["cases"]["character_state"] is False
    assert "reply:timeout" in result["failures"]["character_state"]
    observed = cast(dict[str, object], result["observations"]["character_state"])
    generations = cast(dict[str, dict[str, object]], observed["generations"])
    row = cast(dict[str, object], generations["reply"]["row"])
    assert row["status"] == "pending"
    assert service.deleting


def test_deletion_timeout_does_not_start_residual_checks() -> None:
    service = SyntheticService()
    service.deletion_status = "processing"
    result = execute_synthetic(service)
    assert result["failures"]["author_deletion"] == "deletion_timeout"
    requests = result["requests"]
    deletion_start = next(i for i, r in enumerate(requests) if r["path"] == "/deletion-requests")
    assert all(r["path"].startswith("/deletion-requests") for r in requests[deletion_start:])


def test_three_trials_use_disjoint_registration_ids_and_keep_a_failed_first_trial(
    tmp_path: Path,
) -> None:
    service = SyntheticService()
    service.stale_core = True
    path = tmp_path / "progress.jsonl"
    with httpx.Client(
        transport=httpx.MockTransport(service.handle), base_url="http://synthetic"
    ) as http:
        result = run_e2e_trials(
            case=load_e2e_case(Path("tests/it2/cases/e2e.json")),
            client=http,
            state=service,
            inspection=service,
            author_token="synthetic-e2e-author-only",
            clock=service.clock,
            sleep=service.sleep,
            recorder=JsonlTrialRecorder(path),
        )
    assert result["verdict"] == "FAIL"
    assert [r["trial"] for r in result["trials"]] == [1, 2, 3]
    assert len({r["bank_id"] for r in result["trials"]}) == 3
    assert len(path.read_text().splitlines()) == 3
    assert result["trials"][0]["cases"]["resident_after_change"] is False
    assert all(all(row["cases"].values()) for row in result["trials"][1:])
    for trial in (1, 2, 3):
        prefix = f"it2-e2e-t{trial}-"
        row = result["trials"][trial - 1]
        identifiers = [
            cast(str, payload[field])
            for r in row["requests"]
            if (payload := r["input"]) is not None
            for field in ("session_id", "turn_id", "request_id")
            if field in payload
        ]
        assert prefix + "session-main" in identifiers
        assert all(identifier.startswith(prefix) for identifier in identifiers)


def test_three_complete_http_trials_pass_without_retries(tmp_path: Path) -> None:
    service = SyntheticService()
    path = tmp_path / "progress.jsonl"
    with httpx.Client(
        transport=httpx.MockTransport(service.handle), base_url="http://synthetic"
    ) as http:
        result = run_e2e_trials(
            case=load_e2e_case(Path("tests/it2/cases/e2e.json")),
            client=http,
            state=service,
            inspection=service,
            author_token="synthetic-e2e-author-only",
            clock=service.clock,
            sleep=service.sleep,
            recorder=JsonlTrialRecorder(path),
        )
    assert result["verdict"] == "PASS", result
    assert [row["trial"] for row in result["trials"]] == [1, 2, 3]
    assert [json.loads(line) for line in path.read_text().splitlines()] == result["trials"]


@pytest.mark.parametrize("method", ["provisions", "states", "outbox_count"])
def test_sql_inspection_is_read_only_scoped_and_serializes_state_time(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    module = import_module("tests.it2.e2e_run")
    connection = MagicMock()
    stamp = datetime(2026, 11, 5, tzinfo=UTC)
    row: dict[str, object] = (
        {
            "generated_at": stamp,
            "mood": "穏やか",
            "relationship_temperature": "親しい",
            "current_topic": "お茶",
        }
        if method == "states"
        else {"count": 7}
        if method == "outbox_count"
        else {"provision_id": "synthetic"}
    )
    connection.execute.return_value.fetchall.return_value = [row]
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(module.psycopg, "connect", connect)
    reader = PostgresE2EState("synthetic-database")
    result = getattr(reader, method)("synthetic-key")
    assert connect.call_args.kwargs["options"] == "-c default_transaction_read_only=on"
    sql, parameters = connection.execute.call_args.args
    assert sql.startswith("SELECT ")
    assert parameters == ("synthetic-key",)
    if method == "states":
        assert result[0]["generated_at"] == stamp.isoformat()
        assert result[0]["state"]["mood"] == "穏やか"
    elif method == "outbox_count":
        assert result == 7
    else:
        assert result == [{"provision_id": "synthetic"}]
