"""HTTP ingestion and three independent trials using read-only SDK inspection."""

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import httpx
from hindsight_client import Hindsight

from digital_souls_memory_hindsight.evidence.projection import bank_id
from digital_souls_memory_hindsight.evidence_http import SessionRequest, TurnRequest
from digital_souls_memory_hindsight.recall_http import RecallRequest, RecallResponse

from .cases_contract import Case
from .completion import CompletionResult, read_completion_snapshot, wait_for_completion
from .quality import RecallData, score_quality
from .quality_contract import (
    MemoryRow,
    QualityRun,
    QualityTrial,
    Suite,
    empty_metric_counts,
    quality_suites,
    quality_verdict,
)
from .report import validate_safe
from .scoring import score_trial


@dataclass(frozen=True)
class IngestPlan:
    sessions: list[dict[str, object]]
    turns: list[dict[str, object]]
    recall_requests: dict[str, dict[str, object]]


class IngestPort(Protocol):
    def ingest(self, plan: IngestPlan) -> None: ...
    def recall(self, character: str, request: dict[str, object]) -> RecallData: ...


class CompletionPort(Protocol):
    def wait(self, bank: str) -> CompletionResult: ...


class MemoryReader(Protocol):
    def read(self, bank: str) -> list[MemoryRow]: ...


class TrialRecorder(Protocol):
    def record(self, row: QualityTrial) -> None: ...


class JsonlTrialRecorder:
    def __init__(self, path: Path) -> None:
        self._path = path
        with path.open("x", encoding="utf-8"):
            pass

    def record(self, row: QualityTrial) -> None:
        validate_safe(row)
        serialized = json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
        # Close each row before the next trial, including on process interruption.
        with self._path.open("a", encoding="utf-8") as output:
            output.write(serialized)


def build_ingest_plan(*, cases: Sequence[Case], character_id: str) -> IngestPlan:
    sessions: dict[str, dict[str, object]] = {}
    turns: dict[str, dict[str, object]] = {}
    recalls: dict[str, dict[str, object]] = {}
    started = datetime(2026, 11, 5, tzinfo=UTC)
    for case in cases:
        for session in case["input"]["sessions"]:
            session_id = f"{character_id}-{session['session_id']}"
            sessions[session_id] = SessionRequest.model_validate(
                {
                    "session_id": session_id,
                    "started_at": started,
                    "surface": "IT2",
                    "reports_delivery": False,
                }
            ).model_dump(mode="json")
            for mention in session["mentions"]:
                turn_id = f"{character_id}-{mention['id']}"
                if turn_id in turns:
                    previous = turns[turn_id]
                    if previous["text"] != mention["text"] or previous["session_id"] != session_id:
                        raise ValueError("Shared mention has conflicting input")
                    continue
                turns[turn_id] = TurnRequest.model_validate(
                    {
                        "turn_id": turn_id,
                        "session_id": session_id,
                        "surface": "IT2",
                        "actor": character_id,
                        "kind": "utterance",
                        "text": mention["text"],
                        "perceived_by": [character_id],
                        "addressees": [],
                        "address_mode": "broadcast",
                        "language": "ja",
                        "register": {},
                        "sensitivity": "public",
                        "classifier_version": "it2-fixed-v1",
                        "occurred_at": started + timedelta(minutes=len(turns)),
                    }
                ).model_dump(mode="json", by_alias=True)
        if case["acceptance_id"] in {"AC-M1", "AC-R3"}:
            first = case["input"]["sessions"][0]
            recalls[case["id"]] = RecallRequest.model_validate(
                {
                    "session_id": f"{character_id}-{first['session_id']}",
                    "format": "materials",
                    "query": first["mentions"][0]["text"],
                    "sensitivity": "public",
                    "classifier_version": "it2-fixed-v1",
                }
            ).model_dump(mode="json")
    return IngestPlan(list(sessions.values()), list(turns.values()), recalls)


class HttpIngestPort:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def ingest(self, plan: IngestPlan) -> None:
        for path, rows in (("/sessions", plan.sessions), ("/turns", plan.turns)):
            for row in rows:
                response = self._client.post(path, json=row)
                if response.status_code != 201:
                    raise ValueError("Service did not create synthetic evidence")

    def recall(self, character: str, request: dict[str, object]) -> RecallData:
        response = self._client.post(f"/characters/{character}/recall", json=request)
        if response.status_code != 200:
            raise ValueError("Service did not return recall materials")
        return cast(RecallData, RecallResponse.model_validate(response.json()).model_dump())


async def read_memories(client: Hindsight, bank: str) -> list[MemoryRow]:
    rows: list[MemoryRow] = []
    offset = 0
    while True:
        page = await client.memory.list_memories(
            bank_id=bank,
            limit=100,
            offset=offset,
            _request_timeout=10.0,
        )
        rows.extend(cast(MemoryRow, item.to_dict()) for item in page.items)
        offset += len(page.items)
        if offset >= page.total:
            return rows
        if not page.items:
            raise RuntimeError("Memory enumeration did not advance")


class HindsightQualityPort:
    def __init__(self, client: Hindsight, runner: asyncio.Runner, database_url: str) -> None:
        self._client = client
        self._runner = runner
        self._database_url = database_url

    def wait(self, bank: str) -> CompletionResult:
        return wait_for_completion(
            snapshot=lambda: self._runner.run(
                read_completion_snapshot(self._client, self._database_url, bank)
            ),
            clock=time.monotonic,
            sleep=time.sleep,
        )

    def read(self, bank: str) -> list[MemoryRow]:
        return self._runner.run(read_memories(self._client, bank))


def _run_trial(
    *,
    suite: Suite,
    trial: int,
    cases: list[Case],
    plan: IngestPlan,
    ingest: IngestPort,
    completion: CompletionPort,
    reader: MemoryReader,
    clock: Callable[[], float],
) -> QualityTrial:
    character = f"it2-{suite}-t{trial}"
    bank = bank_id(character)
    started = clock()
    elapsed: float | None = None
    memories: list[MemoryRow] = []
    try:
        ingest.ingest(plan)
        completed = completion.wait(bank)
        elapsed = clock() - started
        if completed.status == "complete":
            memories = reader.read(bank)
            recalls = {
                identifier: ingest.recall(character, request)
                for identifier, request in plan.recall_requests.items()
            }
            scored = score_quality(
                cases=cases,
                character_id=character,
                memories=memories,
                recalls=recalls,
            )
            result = score_trial(
                bank_id=bank,
                completion=completed.status,
                cases=scored["cases"],
                observations=scored["observations"],
            )
            return QualityTrial(
                **result,
                suite=suite,
                trial=trial,
                reason=completed.reason,
                elapsed_seconds=elapsed,
                failures=scored["failures"],
                memories=memories,
            )
    except Exception as error:
        # Trial evidence may contain the exception type, never the external error body.
        completed = CompletionResult("failed", f"execution_error:{type(error).__name__}")
    if elapsed is None:
        elapsed = clock() - started
    result = score_trial(
        bank_id=bank,
        completion=completed.status,
        cases={case["id"]: False for case in cases},
        observations=empty_metric_counts(),
    )
    return QualityTrial(
        **result,
        suite=suite,
        trial=trial,
        reason=completed.reason,
        elapsed_seconds=elapsed,
        failures={case["id"]: completed.reason for case in cases},
        memories=memories,
    )


def run_quality_trials(
    *,
    cases: list[Case],
    ingest: IngestPort,
    completion: CompletionPort,
    reader: MemoryReader,
    clock: Callable[[], float],
    recorder: TrialRecorder,
) -> QualityRun:
    suites = quality_suites(cases)
    plans = {
        (trial, suite): build_ingest_plan(
            cases=selected,
            character_id=f"it2-{suite}-t{trial}",
        )
        for trial in range(1, 4)
        for suite, selected in suites.items()
    }
    trials: list[QualityTrial] = []
    for trial in range(1, 4):
        for suite, selected in suites.items():
            row = _run_trial(
                suite=suite,
                trial=trial,
                cases=selected,
                plan=plans[trial, suite],
                ingest=ingest,
                completion=completion,
                reader=reader,
                clock=clock,
            )
            recorder.record(row)
            trials.append(row)
    return {"verdict": quality_verdict(trials, suites), "trials": trials}
