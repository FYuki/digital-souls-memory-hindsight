"""Supervisor-only performance runner measures the HTTP boundary after ingestion."""

import asyncio
import math
import re
import time
from collections.abc import Callable
from typing import Protocol, TypedDict, cast

import httpx
import psycopg
from hindsight_client import Hindsight

from digital_souls_memory_hindsight.evidence.projection import bank_id
from digital_souls_memory_hindsight.recall_http import RecallRequest, RecallResponse

from .completion import CompletionResult
from .performance import (
    GENERATION_COUNTER,
    Conditions,
    MemoryCounts,
    Sample,
    Series,
    count_memories,
    generation_delta,
    summarize_performance,
    summarize_series,
)
from .quality import RecallData, response_counts
from .quality_run import (
    CompletionPort,
    HttpIngestPort,
    IngestPlan,
    MemoryReader,
    session_payload,
    turn_payload,
)

CHARACTER = "it2-performance"
BATCH_SIZE = 25
ANCHOR_COUNT = 20
REQUEST_COUNT = 200
WARMUP_COUNT = 20


def performance_query(*, index: int, positive: bool) -> str:
    if not 0 <= index < 10000:
        raise ValueError("Synthetic query index exceeds four digits")
    return f"{'性能' if positive else '未知'}資料館{index:04d}番の棚番号を教えて。"


def build_performance_batch(*, start: int, count: int) -> IngestPlan:
    if start < 0 or count <= 0 or start + count > 10000:
        raise ValueError("Synthetic batch exceeds four digits")
    sessions, turns = [], []
    for index in range(start, start + count):
        identifier = f"{CHARACTER}-perf-{index:04d}"
        sessions.append(session_payload(identifier))
        turns.append(
            turn_payload(
                character_id=CHARACTER,
                session_id=identifier,
                turn_id=identifier,
                text=f"性能資料館{index:04d}番の棚番号は{index:04d}番だ。",
                index=index,
            )
        )
    return IngestPlan(sessions, turns, {})


def measure_recall(
    *,
    client: httpx.Client,
    request: dict[str, object],
    positive: bool,
    warmup: bool,
    clock: Callable[[], float],
) -> Sample:
    started = clock()
    try:
        response = client.post(f"/characters/{CHARACTER}/recall", json=request)
    except httpx.HTTPError as error:
        return {
            "elapsed_ms": (clock() - started) * 1000,
            "success": False,
            "positive": positive,
            "fact_nonempty": False,
            "response_nonempty": False,
            "warmup": warmup,
            "error": type(error).__name__,
        }
    elapsed = (clock() - started) * 1000
    sample: Sample = {
        "elapsed_ms": elapsed,
        "success": False,
        "positive": positive,
        "fact_nonempty": False,
        "response_nonempty": False,
        "warmup": warmup,
    }
    if response.status_code != 200:
        sample["error"] = f"HTTP_{response.status_code}"
        return sample
    try:
        data = cast(RecallData, RecallResponse.model_validate(response.json()).model_dump())
    except ValueError as error:
        sample["error"] = type(error).__name__
        return sample
    sample["success"] = True
    sample["fact_nonempty"] = bool(data["memories"])
    sample["response_nonempty"] = not bool(response_counts(data)["empty_responses"])
    return sample


class StateSnapshot(TypedDict):
    statuses: list[str]


def read_state_snapshot(database_url: str) -> StateSnapshot:
    with psycopg.connect(
        database_url, connect_timeout=5, options="-c default_transaction_read_only=on"
    ) as connection:
        rows = connection.execute(
            "SELECT status FROM character_state_outbox WHERE character_id=%s", (CHARACTER,)
        ).fetchall()
    return {"statuses": [str(row[0]) for row in rows]}


def wait_for_quiet(
    *,
    snapshot: Callable[[], StateSnapshot],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> CompletionResult:
    deadline = clock() + 1800
    while True:
        statuses = snapshot()["statuses"]
        remaining = deadline - clock()
        if any(status not in {"pending", "processing", "completed"} for status in statuses):
            return CompletionResult("failed", "character_state_failed")
        if remaining < 0:
            return CompletionResult("timeout", "character_state_timeout")
        if statuses and all(status == "completed" for status in statuses):
            return CompletionResult("complete", "character_state_complete")
        if remaining == 0:
            return CompletionResult("timeout", "character_state_timeout")
        sleep(min(1.0, remaining))


def parse_generation_counter(text: str) -> int | None:
    match = re.search(
        rf"^{re.escape(GENERATION_COUNTER)}(?:\{{[^\n]*\}})?[ \t]+([0-9]+)[ \t]*$",
        text,
        re.MULTILINE,
    )
    if match is None:
        return None
    return int(match[1])


def read_generation_counter(client: httpx.Client) -> int | None:
    try:
        response = client.get("http://llama-generation:8080/metrics")
        response.raise_for_status()
        return parse_generation_counter(response.text)
    except (httpx.HTTPError, ValueError):
        # An unavailable metric is recorded as not_measured, never as zero calls.
        return None


def trace_durations(trace: object, path: str = "trace") -> dict[str, float]:
    result: dict[str, float] = {}
    if isinstance(trace, dict):
        for key, value in trace.items():
            child = f"{path}.{key}"
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and "duration" in str(key).lower()
                and math.isfinite(value)
            ):
                result[child] = float(value)
            elif isinstance(value, (dict, list)):
                result.update(trace_durations(value, child))
    elif isinstance(trace, list):
        for index, value in enumerate(trace):
            result.update(trace_durations(value, f"{path}[{index}]"))
    return result


class StateWaiter(Protocol):
    def wait(self) -> CompletionResult: ...


class PostgresStateWaiter:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def wait(self) -> CompletionResult:
        return wait_for_quiet(
            snapshot=lambda: read_state_snapshot(self._database_url),
            clock=time.monotonic,
            sleep=time.sleep,
        )


class PerformanceRecorder(Protocol):
    def record(self, row: Series) -> None: ...


def prepare_bank(
    *,
    target: int,
    next_index: int,
    ingest: HttpIngestPort,
    completion: CompletionPort,
    reader: MemoryReader,
    state: StateWaiter,
    clock: Callable[[], float],
    extra_inputs: int = 0,
) -> tuple[int, MemoryCounts, dict[str, object]]:
    waits: list[dict[str, object]] = []
    counts: MemoryCounts = {
        "sessions": next_index,
        "input_facts": next_index,
        "extracted_facts": None,
        "consolidated_facts": None,
    }
    try:
        while next_index + extra_inputs < target * 2:
            count = min(BATCH_SIZE, target * 2 - next_index - extra_inputs)
            plan = build_performance_batch(start=next_index, count=count)
            ingest.ingest(plan)
            next_index += count
            counts["sessions"] = next_index
            counts["input_facts"] = next_index + extra_inputs
            wait: dict[str, object] = {}
            waits.append(wait)
            started = clock()
            try:
                completed = completion.wait(bank_id(CHARACTER))
            finally:
                wait["ingestion_seconds"] = clock() - started
            quiet_started = clock()
            try:
                quiet = state.wait() if completed.status == "complete" else completed
            finally:
                wait["character_state_wait_seconds"] = clock() - quiet_started
            if quiet.status != "complete":
                return (
                    next_index,
                    counts,
                    {"status": quiet.status, "reason": quiet.reason, "waits": waits},
                )
            counts = count_memories(
                reader.read(bank_id(CHARACTER)),
                sessions=next_index,
                input_facts=next_index + extra_inputs,
            )
            extracted, consolidated = counts["extracted_facts"], counts["consolidated_facts"]
            if (
                extracted is not None
                and consolidated is not None
                and extracted + consolidated >= target
            ):
                return next_index, counts, {"status": "complete", "waits": waits}
    except Exception as error:
        return (
            next_index,
            counts,
            {
                "status": "failed",
                "reason": f"execution_error:preparation:{type(error).__name__}",
                "waits": waits,
            },
        )
    return (
        next_index,
        counts,
        {"status": "failed", "reason": "memory_target_not_reached", "waits": waits},
    )


def measurement_conditions(model: str) -> Conditions:
    contexts = {"gemma4-12b": 32768, "bge-m3": 8192, "bge-reranker-v2-m3": 8192}
    return {
        "request_count": REQUEST_COUNT,
        "warmup_count": WARMUP_COUNT,
        "concurrency": 1,
        "query_length": len(performance_query(index=0, positive=True)),
        "models": list(contexts),
        "hindsight_generation_model": model,
        "model_gpu_layers": {resident: 999 for resident in contexts},
        "model_context_lengths": contexts,
        "offload": "requested GPU layers",
        "context_length": contexts["gemma4-12b"],
        "format": "assembled",
    }


def register_performance_anchors(client: httpx.Client) -> None:
    for index in range(ANCHOR_COUNT):
        identifier = f"{CHARACTER}-perf-{index:04d}"
        response = client.post(
            f"/characters/{CHARACTER}/verbatim-anchors",
            json={
                "anchor_id": identifier,
                "turn_id": identifier,
                "kind": "commitment",
                "reason": "IT2 synthetic fixture",
                "approved_by": "IT2",
            },
        )
        if response.status_code != 201:
            raise ValueError("Synthetic Anchor registration failed")


def measure_requests(
    *,
    client: httpx.Client,
    next_index: int,
    model: str,
    samples: list[Sample],
    clock: Callable[[], float],
) -> dict[str, object]:
    request = RecallRequest.model_validate(
        {
            "session_id": f"{CHARACTER}-perf-0000",
            "format": "assembled",
            "query": performance_query(index=0, positive=True),
            "sensitivity": "public",
            "classifier_version": "it2-fixed-v1",
        }
    ).model_dump(mode="json")
    before = read_generation_counter(client)
    for index in range(WARMUP_COUNT + REQUEST_COUNT):
        positive = index % 2 == 0
        request["query"] = performance_query(index=index % min(next_index, 100), positive=positive)
        samples.append(
            measure_recall(
                client=client,
                request=request,
                positive=positive,
                warmup=index < WARMUP_COUNT,
                clock=clock,
            )
        )
    return generation_delta(before=before, after=read_generation_counter(client), model=model)


def collect_trace(
    *,
    sdk: Hindsight,
    runner: asyncio.Runner,
    separate: dict[str, object],
) -> dict[str, float]:
    try:
        trace = runner.run(
            sdk.arecall(
                bank_id=bank_id(CHARACTER),
                query=performance_query(index=0, positive=True),
                types=["world", "experience", "observation"],
                include_chunks=True,
                include_source_facts=True,
                trace=True,
            )
        )
        durations = trace_durations(trace.trace)
        separate["trace_status"] = "measured" if durations else "not_measured"
        return durations
    except Exception as error:
        separate["trace_status"] = "not_measured"
        separate["trace_error"] = type(error).__name__
        return {}


def measure_resident(
    *,
    client: httpx.Client,
    clock: Callable[[], float],
    timings: list[float],
) -> None:
    for _ in range(20):
        started = clock()
        response = client.post(
            f"/characters/{CHARACTER}/resident",
            json={"session_id": f"{CHARACTER}-perf-0000", "surface": "IT2"},
        )
        timings.append((clock() - started) * 1000)
        if response.status_code != 200:
            raise ValueError("Resident measurement failed")


def send_character_state_probe(
    *,
    client: httpx.Client,
    target: int,
    next_index: int,
    clock: Callable[[], float],
) -> float:
    started = clock()
    probe = turn_payload(
        character_id=CHARACTER,
        session_id=f"{CHARACTER}-perf-0000",
        turn_id=f"{CHARACTER}-state-probe-{target}",
        text="今は穏やかな気分です。",
        index=next_index,
    )
    response = client.post("/turns", json=probe)
    if response.status_code != 201:
        raise ValueError("CharacterState probe failed")
    return started


def wait_after_measurement(
    *,
    state: StateWaiter,
    completion: CompletionPort,
    started: float,
    clock: Callable[[], float],
    separate: dict[str, object],
) -> bool:
    quiet = state.wait()
    separate.update(
        {
            "character_state_seconds": clock() - started,
            "character_state_scope": "Turn HTTP send through queue completion",
            "character_state_status": quiet.status,
        }
    )
    completed = completion.wait(bank_id(CHARACTER))
    return quiet.status == "complete" and completed.status == "complete"


def run_performance_series(
    *,
    target: int,
    next_index: int,
    anchors_registered: bool,
    extra_inputs: int,
    client: httpx.Client,
    ingest: HttpIngestPort,
    completion: CompletionPort,
    reader: MemoryReader,
    state: StateWaiter,
    sdk: Hindsight,
    runner: asyncio.Runner,
    model: str,
    conditions: Conditions,
    clock: Callable[[], float],
) -> tuple[int, bool, Series]:
    next_index, counts, preparation = prepare_bank(
        target=target,
        next_index=next_index,
        ingest=ingest,
        completion=completion,
        reader=reader,
        state=state,
        clock=clock,
        extra_inputs=extra_inputs,
    )
    samples: list[Sample] = []
    separate: dict[str, object] = {}
    generation = generation_delta(before=None, after=None, model=model)
    durations: dict[str, float] = {}
    if preparation["status"] == "complete":
        stage = "anchor_registration"
        try:
            if not anchors_registered:
                register_performance_anchors(client)
                anchors_registered = True
            stage = "recall_measurement"
            generation = measure_requests(
                client=client, next_index=next_index, model=model, samples=samples, clock=clock
            )
            durations = collect_trace(sdk=sdk, runner=runner, separate=separate)
            stage = "resident_measurement"
            resident_times: list[float] = []
            separate["resident_ms"] = resident_times
            measure_resident(client=client, clock=clock, timings=resident_times)
            stage = "character_state_probe"
            started = send_character_state_probe(
                client=client,
                target=target,
                next_index=next_index,
                clock=clock,
            )
            stage = "post_measurement_wait"
            quiet = wait_after_measurement(
                state=state,
                completion=completion,
                started=started,
                clock=clock,
                separate=separate,
            )
            if not quiet:
                preparation = {
                    **preparation,
                    "status": "failed",
                    "reason": "post_measurement_not_quiet",
                }
        except Exception as error:
            preparation = {
                **preparation,
                "status": "failed",
                "reason": f"execution_error:{stage}:{type(error).__name__}",
            }
    row = summarize_series(
        samples=samples,
        conditions=conditions,
        counts=counts,
        target_memories=target,
        preparation=preparation,
    )
    row["separate_measurements"] = separate
    row["generation"] = generation
    row["trace_durations"] = durations
    return next_index, anchors_registered, row


def run_performance(
    *,
    client: httpx.Client,
    completion: CompletionPort,
    reader: MemoryReader,
    state: StateWaiter,
    sdk: Hindsight,
    runner: asyncio.Runner,
    model: str,
    recorder: PerformanceRecorder,
    clock: Callable[[], float],
) -> dict[str, object]:
    ingest = HttpIngestPort(client)
    series: list[Series] = []
    next_index = 0
    anchors_registered = False
    conditions = measurement_conditions(model)
    for target in (100, 1000):
        next_index, anchors_registered, row = run_performance_series(
            target=target,
            next_index=next_index,
            anchors_registered=anchors_registered,
            extra_inputs=len(series),
            client=client,
            ingest=ingest,
            completion=completion,
            reader=reader,
            state=state,
            sdk=sdk,
            runner=runner,
            model=model,
            conditions=conditions,
            clock=clock,
        )
        recorder.record(row)
        series.append(row)
        if row["preparation"]["status"] != "complete":
            break
    return summarize_performance(series)
