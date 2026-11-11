"""Three fixed R4 trials record SDK candidates and independent service responses."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx
from hindsight_client import Hindsight

from digital_souls_memory_hindsight.anchor_http import AnchorRequest
from digital_souls_memory_hindsight.evidence.projection import bank_id
from digital_souls_memory_hindsight.recall_http import RecallRequest

from .cases_contract import Case
from .completion import CompletionResult
from .quality import QualityResult, RecallData, response_counts
from .quality_contract import empty_metric_counts
from .quality_run import CompletionPort, HttpIngestPort, IngestPlan, build_ingest_plan
from .scoring import score_trial, summarize_trials
from .threshold import AbstentionTrial, Candidate, QueryRecord, score_responses

__all__ = ["response_counts", "score_responses"]


@dataclass(frozen=True)
class AbstentionPlan(IngestPlan):
    anchors: list[dict[str, object]]


class AbstentionIngestPort(Protocol):
    def ingest(self, plan: AbstentionPlan) -> None: ...
    def recall(self, character: str, request: dict[str, object]) -> RecallData: ...


class CandidateReader(Protocol):
    def candidates(self, bank: str, query: str) -> list[Candidate]: ...


class AbstentionRecorder(Protocol):
    def record(self, row: AbstentionTrial) -> None: ...


def build_abstention_plan(*, cases: Sequence[Case], character_id: str) -> AbstentionPlan:
    evidence = build_ingest_plan(cases=cases, character_id=character_id)
    requests: dict[str, dict[str, object]] = {}
    anchors: list[dict[str, object]] = []
    for case in cases:
        session = case["input"]["sessions"][0]
        requests[case["id"]] = RecallRequest.model_validate(
            {
                "session_id": f"{character_id}-{session['session_id']}",
                "query": case["input"]["query"],
                "format": "materials",
                "sensitivity": "public",
                "classifier_version": "it2-fixed-v1",
            }
        ).model_dump(mode="json")
        if case["positive"]:
            anchors.append(
                AnchorRequest.model_validate(
                    {
                        "anchor_id": f"{character_id}-{case['id']}-anchor",
                        "turn_id": f"{character_id}-{session['mentions'][0]['id']}",
                        "kind": "commitment",
                        "reason": "IT2 synthetic fixture",
                        "approved_by": "IT2",
                    }
                ).model_dump()
            )
    return AbstentionPlan(evidence.sessions, evidence.turns, requests, anchors)


class HttpAbstentionPort:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._evidence = HttpIngestPort(client)

    def ingest(self, plan: AbstentionPlan) -> None:
        self._evidence.ingest(plan)
        for anchor in plan.anchors:
            turn = next(row for row in plan.turns if row["turn_id"] == anchor["turn_id"])
            response = self._client.post(
                f"/characters/{turn['actor']}/verbatim-anchors", json=anchor
            )
            if response.status_code != 201:
                raise ValueError("Service did not create synthetic Anchor")

    def recall(self, character: str, request: dict[str, object]) -> RecallData:
        return self._evidence.recall(character, request)


async def read_candidates(client: Hindsight, bank: str, query: str) -> list[Candidate]:
    response = await client.arecall(
        bank_id=bank,
        query=query,
        types=["world", "experience", "observation"],
        include_chunks=True,
        include_source_facts=True,
    )
    chunks, source_facts = response.chunks or {}, response.source_facts or {}
    candidates: list[Candidate] = []
    for result in response.results:
        direct = result.document_id and result.chunk_id in chunks
        sources = (
            [result]
            if direct
            else [source_facts[key] for key in result.source_fact_ids or [] if key in source_facts]
        )
        candidates.append(
            {
                "id": result.id,
                "kind": result.type,
                "text": result.text,
                "reranker_score": None if result.scores is None else result.scores.reranker,
                "sources": [
                    {"document_id": source.document_id, "chunk_text": chunks[source.chunk_id].text}
                    for source in sources
                    if source.document_id and source.chunk_id in chunks
                ],
            }
        )
    return candidates


class HindsightCandidateReader:
    def __init__(self, client: Hindsight, runner: asyncio.Runner) -> None:
        self._client, self._runner = client, runner

    def candidates(self, bank: str, query: str) -> list[Candidate]:
        return self._runner.run(read_candidates(self._client, bank, query))


def run_abstention_trials(
    *,
    cases: list[Case],
    ingest: AbstentionIngestPort,
    completion: CompletionPort,
    reader: CandidateReader,
    clock: Callable[[], float],
    recorder: AbstentionRecorder,
) -> dict[str, object]:
    selected = [case for case in cases if case["acceptance_id"] == "AC-R4"]
    required = {case["id"] for case in selected if case["split"] == "final"}
    trials: list[AbstentionTrial] = []
    for trial in range(1, 4):
        character = f"it2-abstention-t{trial}"
        bank = bank_id(character)
        plan = build_abstention_plan(cases=selected, character_id=character)
        started = clock()
        queries: list[QueryRecord] = []
        scored: QualityResult = {
            "cases": {case["id"]: False for case in selected},
            "failures": {case["id"]: "not_completed" for case in selected},
            "observations": empty_metric_counts(),
        }
        try:
            ingest.ingest(plan)
            completed = completion.wait(bank)
            if completed.status == "complete":
                recalls: dict[str, RecallData] = {}
                for case in selected:
                    response = ingest.recall(character, plan.recall_requests[case["id"]])
                    queries.append(
                        {
                            "case_id": case["id"],
                            "response": response,
                            "candidates": reader.candidates(bank, case["input"]["query"]),
                        }
                    )
                    recalls[case["id"]] = response
                scored = score_responses(cases=selected, character_id=character, recalls=recalls)
        except Exception as error:
            completed = CompletionResult("failed", f"execution_error:{type(error).__name__}")
        result = score_trial(
            bank_id=bank,
            completion=completed.status,
            cases=scored["cases"],
            observations=scored["observations"],
        )
        row = AbstentionTrial(
            **result,
            character_id=character,
            trial=trial,
            reason=completed.reason,
            elapsed_seconds=clock() - started,
            queries=queries,
            failures=scored["failures"],
        )
        recorder.record(row)
        trials.append(row)
    return {
        "verdict": summarize_trials(trials, required_case_ids=required)["verdict"],
        "trials": trials,
    }
