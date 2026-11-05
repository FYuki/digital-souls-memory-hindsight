"""HTTP measurement includes body receipt and synthetic inputs preserve fixed lengths."""

import asyncio
import json
from collections.abc import Iterator
from importlib import import_module
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from digital_souls_memory_hindsight.evidence_http import SessionRequest, TurnRequest
from digital_souls_memory_hindsight.recall_http import RecallRequest
from tests.it2.completion import CompletionResult

pytestmark = pytest.mark.ut


@pytest.fixture
def performance_run() -> ModuleType:
    return import_module("tests.it2.performance_run")


def test_batches_continue_ids_across_hundred_and_thousand_stages(
    performance_run: ModuleType,
) -> None:
    first = performance_run.build_performance_batch(start=0, count=50)
    second = performance_run.build_performance_batch(start=len(first.turns), count=25)
    assert second.turns[0]["turn_id"] == "it2-performance-perf-0050"
    assert {row["turn_id"] for row in first.turns}.isdisjoint(
        row["turn_id"] for row in second.turns
    )
    assert {row["session_id"] for row in first.sessions}.isdisjoint(
        row["session_id"] for row in second.sessions
    )
    for plan in (first, second):
        for row in plan.sessions:
            SessionRequest.model_validate(row)
        for row in plan.turns:
            turn = TurnRequest.model_validate(row)
            assert turn.perceived_by == ["it2-performance"]


@pytest.mark.parametrize("operation", ["batch", "query"])
def test_five_digit_indices_are_rejected_before_sending(
    performance_run: ModuleType, operation: str
) -> None:
    with pytest.raises(ValueError):
        if operation == "batch":
            performance_run.build_performance_batch(start=10000, count=1)
        else:
            performance_run.performance_query(index=10000, positive=True)


def test_queries_have_fixed_length_for_both_polarities(performance_run: ModuleType) -> None:
    queries = [
        performance_run.performance_query(index=index, positive=positive)
        for index in (0, 9, 50, 9999)
        for positive in (False, True)
    ]
    assert len({len(query) for query in queries}) == 1
    assert len(set(queries)) == len(queries)


def test_recall_timer_includes_body_receipt_and_excludes_json_parsing(
    performance_run: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    events: list[str] = []
    response_data = {
        "provision_id": "synthetic",
        "memories": [],
        "source_spans": [],
        "verbatim_anchors": [],
        "context": "合成応答",
    }

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            events.append("body")
            now[0] += 0.4
            yield json.dumps(response_data).encode()

    def respond(request: httpx.Request) -> httpx.Response:
        events.append("send")
        assert request.url.path == "/characters/it2-performance/recall"
        assert RecallRequest.model_validate_json(request.content).format == "assembled"
        now[0] += 0.2
        return httpx.Response(200, stream=Stream())

    original_json = httpx.Response.json

    def parse(response: httpx.Response, **kwargs: object) -> object:
        events.append("parse")
        now[0] += 10
        return original_json(response, **kwargs)

    def clock() -> float:
        events.append("clock")
        return now[0]

    monkeypatch.setattr(httpx.Response, "json", parse)
    with httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client:
        sample = performance_run.measure_recall(
            client=client,
            positive=True,
            warmup=False,
            request={
                "session_id": "synthetic",
                "query": "合成問い合わせ",
                "format": "assembled",
                "sensitivity": "public",
                "classifier_version": "it2-fixed-v1",
            },
            clock=clock,
        )
    assert sample["elapsed_ms"] == pytest.approx(600)
    assert sample["success"] is True
    assert sample["response_nonempty"] is True
    assert sample["fact_nonempty"] is False
    assert events == ["clock", "send", "body", "clock", "parse"]


def test_http_failure_is_a_sample_without_retry(performance_run: ModuleType) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="synthetic-private-error")

    with httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client:
        sample = performance_run.measure_recall(
            client=client,
            positive=True,
            warmup=False,
            request={
                "session_id": "synthetic",
                "query": "合成",
                "format": "assembled",
                "sensitivity": "public",
                "classifier_version": "it2-fixed-v1",
            },
            clock=lambda: 0.0,
        )
    assert sample["success"] is False
    assert sample["fact_nonempty"] is False
    assert sample["response_nonempty"] is False
    assert len(requests) == 1
    assert "synthetic-private-error" not in json.dumps(sample)


@pytest.mark.parametrize("terminal", ["completed", "failed", "timeout"])
def test_quiet_wait_observes_processing_before_terminal_state(
    performance_run: ModuleType, terminal: str
) -> None:
    now = [0.0]
    seen: list[str] = []

    def snapshot() -> dict[str, object]:
        status = "processing" if not seen else ("processing" if terminal == "timeout" else terminal)
        seen.append(status)
        return {"statuses": [status]}

    def sleep(seconds: float) -> None:
        now[0] += 1801 if terminal == "timeout" else seconds

    result = performance_run.wait_for_quiet(snapshot=snapshot, clock=lambda: now[0], sleep=sleep)
    assert (
        result.status
        == {"completed": "complete", "failed": "failed", "timeout": "timeout"}[terminal]
    )
    assert seen[0] == "processing"
    assert len(seen) == 2


def test_memory_preparation_stops_at_input_limit_without_claiming_target(
    performance_run: ModuleType,
) -> None:
    ingest, completion, reader, state = (MagicMock() for _ in range(4))
    completion.wait.return_value = CompletionResult("complete", "synthetic")
    state.wait.return_value = CompletionResult("complete", "synthetic")
    reader.read.return_value = [{"id": "one", "fact_type": "world"}]
    next_index, counts, preparation = performance_run.prepare_bank(
        target=100,
        next_index=0,
        ingest=ingest,
        completion=completion,
        reader=reader,
        state=state,
        clock=lambda: 0.0,
    )
    assert next_index == 200
    assert counts["extracted_facts"] == 1
    assert preparation["status"] == "failed"
    assert ingest.ingest.call_count == 8


@pytest.mark.parametrize("trace_available", [True, False])
@pytest.mark.parametrize("facts_available", [True, False])
def test_runner_registers_anchors_waits_and_keeps_other_measurements_separate(
    performance_run: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    trace_available: bool,
    facts_available: bool,
) -> None:
    events: list[str] = []
    anchors: set[str] = set()
    recall_count = [0]

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/verbatim-anchors"):
            anchors.add(body["anchor_id"])
        elif request.url.path.endswith("/recall"):
            assert len(anchors) == 20
            assert "quiet" in events
            assert body["format"] == "assembled"
            recall_count[0] += 1
            return httpx.Response(
                200,
                json={
                    "provision_id": "synthetic",
                    "memories": [
                        {
                            "id": "f",
                            "content": "合成Fact",
                            "kind": "world",
                            "source_turn_ids": ["t"],
                            "trust": "inferred",
                        }
                    ]
                    if facts_available
                    else [],
                    "source_spans": [
                        {
                            "turn_id": "t",
                            "start": 0,
                            "end": 2,
                            "text": "合成",
                            "actor": "a",
                            "language": "ja",
                            "truncated": False,
                            "original_start": 0,
                            "original_end": 2,
                            "trust": "evidence",
                        }
                    ],
                    "verbatim_anchors": [
                        {"anchor_id": "a", "turn_id": "t", "text": "合成", "trust": "evidence"}
                    ],
                    "context": "合成応答",
                },
            )
        return httpx.Response(200 if request.url.path.endswith("/resident") else 201, json={})

    completion, reader, state, recorder = (MagicMock() for _ in range(4))
    completion.wait.return_value = CompletionResult("complete", "synthetic")

    def quiet() -> CompletionResult:
        events.append("quiet")
        return CompletionResult("complete", "synthetic")

    state.wait.side_effect = quiet
    reader.read.side_effect = [
        [{"id": str(i), "fact_type": "world"} for i in range(count)] for count in (100, 1000)
    ]
    sdk = MagicMock()
    sdk.arecall = (
        AsyncMock(return_value=MagicMock(trace={"keyword_duration": 0.01}))
        if trace_available
        else AsyncMock(side_effect=TimeoutError("synthetic"))
    )
    monkeypatch.setattr(performance_run, "read_generation_counter", lambda client: 10)
    with (
        httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client,
        asyncio.Runner() as runner,
    ):
        result = performance_run.run_performance(
            client=client,
            completion=completion,
            reader=reader,
            state=state,
            sdk=sdk,
            runner=runner,
            model="gemma4-12b",
            recorder=recorder,
            clock=lambda: 0.0,
        )
    assert result["verdict"] == ("PASS" if facts_available else "FAIL")
    assert recall_count[0] == 440
    assert len(result["series"]) == 2
    for row in result["series"]:
        assert row["request_count"] == 200
        assert len(row["samples"]) == 220
        assert len(row["separate_measurements"]["resident_ms"]) == 20
        assert row["separate_measurements"]["character_state_status"] == "complete"
        assert row["generation"]["counter_delta"] == 0
        assert row["generation"]["counter"] == "llamacpp:n_decode_total"
        assert row["positive_fact_responses"] == (100 if facts_available else 0)
        assert row["positive_nonempty_responses"] == 100
        assert row["trace_durations"] == (
            {"trace.keyword_duration": 0.01} if trace_available else {}
        )
        assert row["separate_measurements"]["trace_status"] == (
            "measured" if trace_available else "not_measured"
        )
    assert len(recorder.record.call_args_list) == 2


@pytest.mark.parametrize(
    "metric,expected",
    [
        ("llamacpp:n_decode_total 12\n", 12),
        ("other 10", None),
        ("llamacpp:n_decode_total NaN", None),
        ("llamacpp:n_decode_total 1e+06", None),
        ("llamacpp:n_decode_total 12.0", None),
        ("llamacpp:n_decode_total -1", None),
        ("llamacpp:n_decode_total １２", None),
        ("llamacpp:requests_total 12", None),
        ("llamacpp:n_decode_total 9007199254740993", 9007199254740993),
    ],
)
def test_generation_metric_missing_is_not_zero(
    performance_run: ModuleType, metric: str, expected: int | None
) -> None:
    assert performance_run.parse_generation_counter(metric) == expected


def test_state_snapshot_uses_read_only_character_scoped_sql(
    performance_run: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [("processing",), ("completed",)]
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(performance_run.psycopg, "connect", connect)
    snapshot = performance_run.read_state_snapshot("synthetic-db")
    assert snapshot["statuses"] == ["processing", "completed"]
    assert connect.call_args.kwargs["options"] == "-c default_transaction_read_only=on"
    assert connection.execute.call_args.args[1] == ("it2-performance",)
    assert (
        "SELECT status FROM character_state_outbox WHERE character_id=%s"
        == connection.execute.call_args.args[0]
    )


def test_state_waiter_reads_fresh_snapshots(
    performance_run: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = MagicMock(return_value={"statuses": ["completed"]})
    monkeypatch.setattr(performance_run, "read_state_snapshot", snapshot)
    assert performance_run.PostgresStateWaiter("synthetic-db").wait().status == "complete"
    snapshot.assert_called_once_with("synthetic-db")


def test_quiet_failure_prevents_memory_read_and_measurement(performance_run: ModuleType) -> None:
    ingest, completion, reader, state = (MagicMock() for _ in range(4))
    completion.wait.return_value = CompletionResult("complete", "synthetic")
    state.wait.return_value = CompletionResult("timeout", "synthetic timeout")
    next_index, counts, preparation = performance_run.prepare_bank(
        target=100,
        next_index=0,
        ingest=ingest,
        completion=completion,
        reader=reader,
        state=state,
        clock=lambda: 0.0,
    )
    assert next_index == 25
    assert preparation["status"] == "timeout"
    assert counts["extracted_facts"] is None
    reader.read.assert_not_called()
    assert ingest.ingest.call_count == 1


@pytest.mark.parametrize(
    "status,text,expected",
    [(200, "llamacpp:n_decode_total 12", 12), (200, "other 10", None), (503, "error", None)],
)
def test_generation_counter_read_distinguishes_unavailable_metric(
    performance_run: ModuleType, status: int, text: str, expected: int | None
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://llama-generation:8080/metrics"
        return httpx.Response(status, text=text)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert performance_run.read_generation_counter(client) == expected


def test_trace_durations_keep_paths_and_ignore_non_numeric_values(
    performance_run: ModuleType,
) -> None:
    result = performance_run.trace_durations(
        {
            "keyword": {"duration_ms": 4},
            "paths": [{"duration": 2.0}, {"duration": "unavailable"}],
            "flag_duration": True,
        }
    )
    assert result == {"trace.keyword.duration_ms": 4.0, "trace.paths[0].duration": 2.0}


def test_runner_stops_before_recalls_when_state_queue_times_out(
    performance_run: ModuleType,
) -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(201, json={})

    completion, reader, state, sdk, recorder = (MagicMock() for _ in range(5))
    completion.wait.return_value = CompletionResult("complete", "synthetic")
    state.wait.return_value = CompletionResult("timeout", "synthetic timeout")
    with (
        httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client,
        asyncio.Runner() as runner,
    ):
        result = performance_run.run_performance(
            client=client,
            completion=completion,
            reader=reader,
            state=state,
            sdk=sdk,
            runner=runner,
            model="gemma4-12b",
            recorder=recorder,
            clock=lambda: 0.0,
        )
    assert result["verdict"] == "FAIL"
    assert len(result["series"]) == 1
    assert result["series"][0]["samples"] == []
    assert set(paths) == {"/sessions", "/turns"}
    reader.read.assert_not_called()
    assert recorder.record.call_count == 1


@pytest.mark.parametrize("materials", ["empty", "context", "anchor", "span", "fact"])
def test_measurement_distinguishes_facts_from_other_response_materials(
    performance_run: ModuleType, materials: str
) -> None:
    data: dict[str, object] = {
        "provision_id": "synthetic",
        "memories": [],
        "source_spans": [],
        "verbatim_anchors": [],
        "context": None,
    }
    if materials == "context":
        data["context"] = "合成"
    elif materials == "anchor":
        data["verbatim_anchors"] = [
            {"anchor_id": "a", "turn_id": "t", "text": "合成", "trust": "evidence"}
        ]
    elif materials == "span":
        data["source_spans"] = [
            {
                "turn_id": "t",
                "start": 0,
                "end": 2,
                "text": "合成",
                "actor": "a",
                "language": "ja",
                "truncated": False,
                "original_start": 0,
                "original_end": 2,
                "trust": "evidence",
            }
        ]
    elif materials == "fact":
        data["memories"] = [
            {
                "id": "f",
                "content": "合成",
                "kind": "world",
                "source_turn_ids": ["t"],
                "trust": "inferred",
            }
        ]
    with httpx.Client(
        base_url="http://service",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)),
    ) as client:
        sample = performance_run.measure_recall(
            client=client,
            request={},
            positive=True,
            warmup=False,
            clock=lambda: 0.0,
        )
    assert sample["success"] is True
    assert sample["fact_nonempty"] == (materials == "fact")
    assert sample["response_nonempty"] == (materials != "empty")


@pytest.mark.parametrize(
    "failure",
    [
        "anchor_registration",
        "resident_measurement",
        "character_state_probe",
        "post_measurement_wait",
        "preparation",
        "post_measurement_not_quiet",
    ],
)
def test_stage_failures_preserve_completed_measurements_and_stop(
    performance_run: ModuleType, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    paths: list[str] = []
    resident_count = [0]

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path.endswith("/verbatim-anchors"):
            return httpx.Response(503 if failure == "anchor_registration" else 201)
        if request.url.path.endswith("/recall"):
            return httpx.Response(
                200,
                json={
                    "provision_id": "synthetic",
                    "memories": [
                        {
                            "id": "f",
                            "content": "合成",
                            "kind": "world",
                            "source_turn_ids": ["t"],
                            "trust": "inferred",
                        }
                    ],
                    "source_spans": [],
                    "verbatim_anchors": [],
                    "context": "合成",
                },
            )
        if request.url.path.endswith("/resident"):
            resident_count[0] += 1
            return httpx.Response(503 if failure == "resident_measurement" else 200)
        if request.url.path == "/turns":
            if failure == "preparation" and body["turn_id"].endswith("0025"):
                return httpx.Response(503, text="synthetic-private-error")
            if "state-probe" in body["turn_id"] and failure == "character_state_probe":
                return httpx.Response(503, text="synthetic-private-error")
        return httpx.Response(201)

    completion, reader, state, sdk, recorder = (MagicMock() for _ in range(5))
    completion.wait.return_value = CompletionResult("complete", "synthetic")
    state.wait.side_effect = [
        CompletionResult("complete", "synthetic"),
        RuntimeError("synthetic-private-error")
        if failure == "post_measurement_wait"
        else CompletionResult(
            "timeout" if failure == "post_measurement_not_quiet" else "complete", "synthetic"
        ),
    ]
    reader.read.return_value = [
        {"id": str(i), "fact_type": "world"} for i in range(1 if failure == "preparation" else 100)
    ]
    sdk.arecall = AsyncMock(return_value=MagicMock(trace={}))
    monkeypatch.setattr(performance_run, "read_generation_counter", lambda client: 10)
    with (
        httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client,
        asyncio.Runner() as runner,
    ):
        result = performance_run.run_performance(
            client=client,
            completion=completion,
            reader=reader,
            state=state,
            sdk=sdk,
            runner=runner,
            model="gemma4-12b",
            recorder=recorder,
            clock=lambda: 1.0,
        )
    assert result["verdict"] == "FAIL"
    assert len(result["series"]) == 1
    row = result["series"][0]
    assert row["verdict"] == "FAIL"
    assert row["preparation"]["status"] == "failed"
    assert failure in row["preparation"]["reason"]
    assert row["preparation"]["waits"] == [
        {"ingestion_seconds": 0.0, "character_state_wait_seconds": 0.0}
    ]
    assert "synthetic-private-error" not in json.dumps(row)
    recorder.record.assert_called_once_with(row)
    if failure in {"preparation", "anchor_registration"}:
        assert row["samples"] == []
        assert not any(path.endswith("/recall") for path in paths)
        if failure == "preparation":
            assert row["counts"]["sessions"] == 25
            assert row["counts"]["input_facts"] == 25
            assert row["counts"]["extracted_facts"] == 1
            assert row["preparation"]["reason"] == "execution_error:preparation:ValueError"
    else:
        assert len(row["samples"]) == 220
        assert row["generation"]["counter_delta"] == 0
        assert len(row["separate_measurements"]["resident_ms"]) == resident_count[0]
        assert paths.count("/characters/it2-performance/resident") == resident_count[0]
