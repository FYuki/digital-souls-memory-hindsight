"""HTTP ingestion and fixed trials with isolated boundary doubles."""

import asyncio
import json
from collections import Counter
from importlib import import_module
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from hindsight_client import Hindsight
from hindsight_client_api.models.list_memory_units_response import ListMemoryUnitsResponse
from hindsight_client_api.models.memory_unit_list_item import MemoryUnitListItem

from digital_souls_memory_hindsight.evidence.projection import bank_id, document_id
from digital_souls_memory_hindsight.evidence_http import SessionRequest, TurnRequest
from digital_souls_memory_hindsight.recall_http import RecallRequest, RecallResponse
from tests.it2.cases_contract import Case, load_cases
from tests.it2.completion import CompletionResult, CompletionSnapshot
from tests.it2.quality_contract import QualityTrial

pytestmark = pytest.mark.ut
CORPUS = Path(__file__).resolve().parents[1] / "it2/cases"


@pytest.fixture
def quality_run() -> ModuleType:
    return import_module("tests.it2.quality_run")


def _cases(acceptance: str) -> list[Case]:
    return [case for case in load_cases(CORPUS) if case["acceptance_id"] == acceptance]


def test_trial_plans_use_disjoint_session_and_turn_ids(quality_run: ModuleType) -> None:
    first = quality_run.build_ingest_plan(cases=_cases("AC-M1"), character_id="it2-same-fact-t1")
    second = quality_run.build_ingest_plan(cases=_cases("AC-M1"), character_id="it2-same-fact-t2")
    assert first.turns[0]["turn_id"] == "it2-same-fact-t1-same-m1"
    assert second.turns[0]["turn_id"] == "it2-same-fact-t2-same-m1"
    assert {row["turn_id"] for row in first.turns}.isdisjoint(
        row["turn_id"] for row in second.turns
    )
    assert {row["session_id"] for row in first.sessions}.isdisjoint(
        row["session_id"] for row in second.sessions
    )


def test_shared_japanese_mentions_are_ingested_once(quality_run: ModuleType) -> None:
    plan = quality_run.build_ingest_plan(cases=_cases("AC-R3"), character_id="it2-japanese-t1")
    assert len(plan.sessions) == 3
    assert Counter(row["turn_id"] for row in plan.turns) == {
        f"it2-japanese-t1-ja-{feature}-m1": 1 for feature in ("date", "proper_name", "verbatim")
    }


def test_conflicting_shared_mention_stops_before_http(quality_run: ModuleType) -> None:
    cases = _cases("AC-R3")
    conflicting: Case = {
        **cases[1],
        "input": {
            "sessions": [
                {
                    "session_id": "ja-proper_name-s1",
                    "mentions": [{"id": "ja-proper_name-m1", "text": "異なる合成本文"}],
                }
            ]
        },
    }
    with pytest.raises(ValueError):
        quality_run.build_ingest_plan(cases=[cases[0], conflicting], character_id="it2-japanese-t1")


def test_http_ingestion_preserves_inputs_and_uses_valid_service_payloads(
    quality_run: ModuleType,
) -> None:
    cases = _cases("AC-M1")
    cases = [{**case, "expected": {"facts": ["synthetic-answer-never-send"]}} for case in cases]
    plan = quality_run.build_ingest_plan(cases=cases, character_id="it2-same-fact-t1")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/sessions":
            session = SessionRequest.model_validate(body)
            assert session.reports_delivery is False
        elif request.url.path == "/turns":
            turn = TurnRequest.model_validate(body)
            assert turn.actor == "it2-same-fact-t1"
            assert turn.perceived_by == [turn.actor]
            assert turn.address_mode == "broadcast"
            assert turn.kind == "utterance"
        else:
            query = RecallRequest.model_validate(body)
            assert query.format == "materials"
        return httpx.Response(
            200 if request.url.path.endswith("/recall") else 201,
            json={
                "provision_id": "synthetic",
                "memories": [],
                "source_spans": [],
                "verbatim_anchors": [],
                "context": None,
            },
        )

    with httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client:
        port = quality_run.HttpIngestPort(client)
        port.ingest(plan)
        port.recall("it2-same-fact-t1", plan.recall_requests[cases[0]["id"]])
    sessions: set[str] = set()
    posted_texts = []
    for request in requests:
        assert request.method == "POST"
        body = json.loads(request.content)
        assert "synthetic-answer-never-send" not in request.content.decode()
        if request.url.path == "/sessions":
            sessions.add(body["session_id"])
        elif request.url.path == "/turns":
            assert body["session_id"] in sessions
            posted_texts.append(body["text"])
        else:
            assert request.url.path == "/characters/it2-same-fact-t1/recall"
            assert body["query"] in posted_texts
    assert posted_texts == [
        mention["text"]
        for case in cases
        for session in case["input"]["sessions"]
        for mention in session["mentions"]
    ]


@pytest.mark.parametrize("status", [200, 409, 503])
def test_http_ingestion_rejects_non_created_without_retry(
    quality_run: ModuleType, status: int
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={})

    plan = quality_run.build_ingest_plan(cases=_cases("AC-M1"), character_id="it2-same-fact-t1")
    with httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((ValueError, httpx.HTTPError)):
            quality_run.HttpIngestPort(client).ingest(plan)
    assert len(requests) == 1


class Boundaries:
    """Record port effects; no retain, bank configuration or real network is available."""

    def __init__(self, first: str) -> None:
        self.first = first
        self.events: list[tuple[str, str]] = []
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def ingest(self, plan: object) -> None:
        self.events.append(("ingest", ""))
        self.now += 2
        if self.first == "exception" and len(self.events) == 1:
            raise RuntimeError("synthetic-private-exception-body")

    def wait(self, bank: str) -> CompletionResult:
        self.events.append(("wait", bank))
        self.now += 5
        if self.first == "timeout" and sum(event == "wait" for event, _ in self.events) == 1:
            return CompletionResult("timeout", "completion wait limit exceeded")
        return CompletionResult("complete", "retain, consolidation and outbox completed")

    def read(self, bank: str) -> list[dict[str, object]]:
        self.events.append(("read", bank))
        self.now += 100
        return []

    def recall(self, character: str, request: dict[str, object]) -> dict[str, object]:
        self.events.append(("recall", bank_id(character)))
        self.now += 100
        return {"memories": [], "source_spans": [], "verbatim_anchors": [], "context": None}


class SuccessfulBoundaries(Boundaries):
    def read(self, bank: str) -> list[dict[str, object]]:
        super().read(bank)
        character, acceptance = next(
            (f"it2-{suite}-t{trial}", acceptance)
            for trial in range(1, 4)
            for suite, acceptance in (
                ("same-fact", "AC-M1"),
                ("difference-fact", "AC-M2"),
                ("japanese", "AC-R3"),
            )
            if bank_id(f"it2-{suite}-t{trial}") == bank
        )
        rows: dict[str, dict[str, object]] = {}
        for case in _cases(acceptance):
            sources = []
            for session in case["input"]["sessions"]:
                for mention in session["mentions"]:
                    identifier = f"{character}-{mention['id']}"
                    sources.append(identifier)
                    rows[identifier] = dict(
                        MemoryUnitListItem(
                            id=identifier,
                            text=mention["text"],
                            fact_type="world",
                            document_id=document_id(f"{character}-{session['session_id']}"),
                        ).to_dict()
                    )
                    if acceptance == "AC-M2":
                        rows[identifier + "-observation"] = dict(
                            MemoryUnitListItem(
                                id=identifier + "-observation",
                                text=mention["text"],
                                fact_type="observation",
                                source_memory_ids=[identifier],
                            ).to_dict()
                        )
            if acceptance == "AC-M1":
                identifier = f"{character}-{case['id']}-observation"
                rows[identifier] = dict(
                    MemoryUnitListItem(
                        id=identifier,
                        text="澪の勤務先は星見町の天文台",
                        fact_type="observation",
                        source_memory_ids=sources,
                    ).to_dict()
                )
        return list(rows.values())

    def recall(self, character: str, request: dict[str, object]) -> dict[str, object]:
        super().recall(character, request)
        case = next(
            case
            for case in load_cases(CORPUS)
            if case["acceptance_id"] in {"AC-M1", "AC-R3"}
            and request["query"] == case["input"]["sessions"][0]["mentions"][0]["text"]
        )
        mentions = [
            mention for session in case["input"]["sessions"] for mention in session["mentions"]
        ]
        turns = [f"{character}-{mention['id']}" for mention in mentions]
        identifier = (
            f"{character}-{case['id']}-observation"
            if case["acceptance_id"] == "AC-M1"
            else turns[0]
        )
        return RecallResponse.model_validate(
            {
                "provision_id": "synthetic-provision",
                "context": None,
                "verbatim_anchors": [],
                "memories": [
                    {
                        "id": identifier,
                        "kind": "observation",
                        "content": "合成 Fact",
                        "source_turn_ids": turns,
                        "trust": "inferred",
                    }
                ],
                "source_spans": [
                    {
                        "turn_id": turn,
                        "text": mention["text"],
                        "start": 0,
                        "end": len(mention["text"]),
                        "actor": character,
                        "language": "ja",
                        "truncated": False,
                        "original_start": 0,
                        "original_end": len(mention["text"]),
                        "trust": "evidence",
                    }
                    for turn, mention in zip(turns, mentions, strict=True)
                ],
            }
        ).model_dump()


@pytest.mark.parametrize("first", ["complete", "timeout", "exception"])
def test_fixed_trials_preserve_failures_wait_before_read_and_measure_only_ingestion(
    quality_run: ModuleType, first: str, tmp_path: Path
) -> None:
    ports = SuccessfulBoundaries(first)
    path = tmp_path / "progress.jsonl"
    file_recorder = quality_run.JsonlTrialRecorder(path)

    class Recorder:
        def record(self, row: QualityTrial) -> None:
            file_recorder.record(row)
            ports.events.append(("record", row["bank_id"]))

    result = quality_run.run_quality_trials(
        cases=load_cases(CORPUS),
        ingest=ports,
        completion=ports,
        reader=ports,
        clock=ports.clock,
        recorder=Recorder(),
    )
    trials = result["trials"]
    saved = [json.loads(line) for line in path.read_text().split("\n") if line]
    assert saved == trials
    assert "synthetic-private-exception-body" not in json.dumps(saved)
    ingest_and_record = [event for event, _ in ports.events if event in {"ingest", "record"}]
    assert ingest_and_record == [event for _ in range(9) for event in ("ingest", "record")]
    assert [(row["trial"], row["suite"]) for row in trials] == [
        (trial, suite)
        for trial in range(1, 4)
        for suite in ("same-fact", "difference-fact", "japanese")
    ]
    assert {row["bank_id"] for row in trials} == {
        bank_id(f"it2-{suite}-t{trial}")
        for trial in range(1, 4)
        for suite in ("same-fact", "difference-fact", "japanese")
    }
    assert sum(event == "ingest" for event, _ in ports.events) == 9
    for index, row in enumerate(trials):
        acceptance = {"same-fact": "AC-M1", "difference-fact": "AC-M2", "japanese": "AC-R3"}[
            row["suite"]
        ]
        assert set(row["cases"]) == {case["id"] for case in _cases(acceptance)}
        assert row["elapsed_seconds"] == (2 if index == 0 and first == "exception" else 7)
    completed_banks: set[str] = set()
    for event, bank in ports.events:
        if event == "wait" and not (first == "timeout" and bank == trials[0]["bank_id"]):
            completed_banks.add(bank)
        if event in {"read", "recall"}:
            assert bank in completed_banks
    assert result["verdict"] == ("PASS" if first == "complete" else "FAIL")
    assert all(row["completion"] == "complete" and all(row["cases"].values()) for row in trials[1:])
    if first != "complete":
        failed = trials[0]
        assert failed["completion"] == ("timeout" if first == "timeout" else "failed")
        assert failed["metrics"] is None
        assert not any(failed["cases"].values())
        assert failed["reason"] == (
            "completion wait limit exceeded"
            if first == "timeout"
            else "execution_error:RuntimeError"
        )
        assert all(bank != failed["bank_id"] for event, bank in ports.events if event == "read")


def test_recorder_rejects_unsafe_row_without_appending(
    quality_run: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "progress.jsonl"
    recorder = quality_run.JsonlTrialRecorder(path)
    row: QualityTrial = {
        "suite": "same-fact",
        "trial": 1,
        "bank_id": "synthetic-bank",
        "completion": "timeout",
        "reason": "synthetic safe reason",
        "elapsed_seconds": 1.0,
        "cases": {"same-fact-001": False},
        "failures": {"same-fact-001": "timeout"},
        "metrics": None,
        "memories": [],
    }
    recorder.record(row)
    original = path.read_text()
    with pytest.raises(ValueError):
        recorder.record({**row, "reason": "Bearer synthetic-credential"})
    assert path.read_text() == original
    assert json.loads(original) == row


def test_recording_error_stops_before_the_next_trial(quality_run: ModuleType) -> None:
    ports = SuccessfulBoundaries("complete")

    class FailingRecorder:
        def record(self, row: QualityTrial) -> None:
            raise OSError("synthetic-write-failure")

    with pytest.raises(OSError):
        quality_run.run_quality_trials(
            cases=load_cases(CORPUS),
            ingest=ports,
            completion=ports,
            reader=ports,
            clock=ports.clock,
            recorder=FailingRecorder(),
        )
    assert sum(event == "ingest" for event, _ in ports.events) == 1


def test_memory_reader_consumes_all_sdk_pages_and_preserves_lineage(
    quality_run: ModuleType,
) -> None:
    client = MagicMock(spec=Hindsight)
    first = MemoryUnitListItem(id="f1", text="合成 Fact", fact_type="world", document_id="doc1")
    second = MemoryUnitListItem(
        id="o1",
        text="合成統合",
        fact_type="observation",
        source_memory_ids=["f1"],
    )
    client.memory.list_memories = AsyncMock(
        side_effect=[
            ListMemoryUnitsResponse(items=[first], total=2, limit=100, offset=0),
            ListMemoryUnitsResponse(items=[second], total=2, limit=100, offset=1),
        ]
    )
    rows = asyncio.run(quality_run.read_memories(client, "synthetic-bank"))
    assert rows == [first.to_dict(), second.to_dict()]
    calls = client.memory.list_memories.await_args_list
    assert [call.kwargs["offset"] for call in calls] == [0, 1]
    assert all(call.kwargs["bank_id"] == "synthetic-bank" for call in calls)
    assert all(call.kwargs["limit"] == 100 for call in calls)


def test_memory_reader_rejects_a_page_that_does_not_advance(quality_run: ModuleType) -> None:
    client = MagicMock(spec=Hindsight)
    client.memory.list_memories = AsyncMock(
        return_value=ListMemoryUnitsResponse(
            items=[],
            total=1,
            limit=100,
            offset=0,
        )
    )
    with pytest.raises(RuntimeError):
        asyncio.run(quality_run.read_memories(client, "synthetic-bank"))
    assert client.memory.list_memories.await_count == 1


def test_sdk_port_connects_shared_completion_and_memory_reading(
    quality_run: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MagicMock(spec=Hindsight)
    snapshot: CompletionSnapshot = {
        "outbox": [{"operation_id": "retain", "status": "completed", "update_mode": "append"}],
        "operations": [
            {"operation_id": "retain", "status": "completed", "type": "retain"},
            {"operation_id": "consolidate", "status": "completed", "type": "consolidation"},
        ],
        "pending_consolidation": 0,
        "failed_consolidation": 0,
    }
    read_snapshot = AsyncMock(return_value=snapshot)
    read_memory = AsyncMock(return_value=[{"id": "synthetic-fact"}])
    monkeypatch.setattr(quality_run, "read_completion_snapshot", read_snapshot)
    monkeypatch.setattr(quality_run, "read_memories", read_memory)
    with asyncio.Runner() as runner:
        port = quality_run.HindsightQualityPort(client, runner, "synthetic-database")
        assert port.wait("synthetic-bank").status == "complete"
        assert port.read("synthetic-bank") == [{"id": "synthetic-fact"}]
        read_snapshot.assert_awaited_once_with(client, "synthetic-database", "synthetic-bank")
        read_memory.assert_awaited_once_with(client, "synthetic-bank")
        read_snapshot.side_effect = RuntimeError("synthetic-snapshot-error")
        with pytest.raises(RuntimeError):
            port.wait("synthetic-bank")
        assert read_snapshot.await_count == 2
