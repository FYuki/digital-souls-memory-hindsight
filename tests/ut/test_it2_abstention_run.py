"""Inspect SDK scores without changing recall or the independent Anchor path."""

import asyncio
import json
from importlib import import_module
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from hindsight_client import Hindsight
from hindsight_client_api.models.recall_response import RecallResponse

from digital_souls_memory_hindsight.evidence.projection import document_id
from digital_souls_memory_hindsight.hindsight_recall import HindsightRecall
from tests.it2.cases_contract import Case, load_cases
from tests.it2.completion import CompletionResult
from tests.it2.quality import RecallData

pytestmark = pytest.mark.ut
CORPUS = Path(__file__).resolve().parents[1] / "it2/cases"


@pytest.fixture
def abstention() -> ModuleType:
    return import_module("tests.it2.abstention_run")


def _sdk_response() -> RecallResponse:
    return RecallResponse.model_validate(
        {
            "results": [
                {
                    "id": "boundary",
                    "type": "world",
                    "text": "合成 Fact",
                    "document_id": "doc",
                    "chunk_id": "chunk",
                    "scores": {"reranker": 0.5, "final": 1.0},
                },
                {
                    "id": "below",
                    "type": "experience",
                    "text": "合成 Fact",
                    "document_id": "doc",
                    "chunk_id": "chunk",
                    "scores": {"reranker": 0.49, "final": 1.0},
                },
                {
                    "id": "missing",
                    "type": "world",
                    "text": "合成 Fact",
                    "document_id": "doc",
                    "chunk_id": "chunk",
                    "scores": {"reranker": None, "final": 1.0},
                },
                {
                    "id": "observation",
                    "type": "observation",
                    "text": "合成統合",
                    "source_fact_ids": ["source"],
                    "scores": {"reranker": 0.9, "final": 1.0},
                },
                {
                    "id": "unresolved",
                    "type": "world",
                    "text": "出典なし",
                    "scores": {"reranker": 0.99, "final": 1.0},
                },
            ],
            "chunks": {"chunk": {"id": "chunk", "text": "合成原文", "chunk_index": 0}},
            "source_facts": {
                "source": {
                    "id": "source",
                    "type": "world",
                    "text": "合成 Fact",
                    "document_id": "doc",
                    "chunk_id": "chunk",
                }
            },
        }
    )


def test_reader_records_actual_scores_and_uses_service_sdk_arguments(
    abstention: ModuleType,
) -> None:
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(return_value=_sdk_response())
    candidates = asyncio.run(abstention.read_candidates(client, "bank", "合成 query"))
    recorded = client.arecall.await_args.kwargs
    actual = asyncio.run(
        HindsightRecall(client, min_reranker_score=0.5).recall("bank", "合成 query")
    )
    assert recorded == client.arecall.await_args.kwargs
    assert {row["id"]: row["reranker_score"] for row in candidates} == {
        "boundary": 0.5,
        "below": 0.49,
        "missing": None,
        "observation": 0.9,
        "unresolved": 0.99,
    }
    threshold = import_module("tests.it2.threshold")
    kept = threshold.filter_candidates(candidates, threshold=0.5)
    assert [row["id"] for row in kept] == [fact.id for fact in actual]
    assert [row["id"] for row in kept] == ["boundary", "observation"]


@pytest.mark.parametrize("remaining", ["anchor", "source", "context", "nothing", "fact"])
def test_empty_fact_and_empty_response_are_distinct(abstention: ModuleType, remaining: str) -> None:
    response: RecallData = {
        "memories": [{"id": "f", "source_turn_ids": ["turn"]}] if remaining == "fact" else [],
        "source_spans": [{"turn_id": "turn", "text": "合成"}] if remaining == "source" else [],
        "verbatim_anchors": [{"id": "a"}] if remaining == "anchor" else [],
        "context": "合成" if remaining == "context" else None,
    }
    counts = abstention.response_counts(response)
    assert counts["empty_fact_responses"] == int(remaining != "fact")
    assert counts["empty_responses"] == int(remaining == "nothing")


def test_negative_anchor_contamination_is_recorded_without_failing_case(
    abstention: ModuleType,
) -> None:
    case = next(
        case
        for case in load_cases(CORPUS)
        if case["acceptance_id"] == "AC-R4" and not case["positive"]
    )
    result = abstention.score_responses(
        cases=[case],
        character_id="it2-abstention-t1",
        recalls={
            case["id"]: {
                "memories": [],
                "source_spans": [],
                "verbatim_anchors": [{"id": "unrelated"}],
                "context": None,
            }
        },
    )
    assert result["cases"] == {case["id"]: True}
    assert result["observations"]["negative_returned_anchors"] == 1
    assert result["observations"]["unrelated_negative_anchors"] == 1
    assert result["observations"]["empty_fact_responses"] == 1
    assert result["observations"]["empty_responses"] == 0


def test_positive_correctness_accepts_lineage_of_duplicate_input_text(
    abstention: ModuleType,
) -> None:
    cases = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    positive = next(case for case in cases if case["positive"])
    matching_text = positive["expected"]["facts"][0]
    duplicate = next(
        mention["id"]
        for case in cases
        if not case["positive"]
        for session in case["input"]["sessions"]
        for mention in session["mentions"]
        if mention["text"] == matching_text
    )
    result = abstention.score_responses(
        cases=cases,
        character_id="it2-abstention-t1",
        recalls={
            case["id"]: {
                "memories": (
                    [{"id": "f", "source_turn_ids": [f"it2-abstention-t1-{duplicate}"]}]
                    if case["id"] == positive["id"]
                    else []
                ),
                "source_spans": [],
                "verbatim_anchors": [],
                "context": None,
            }
            for case in cases
        },
    )
    assert result["cases"][positive["id"]] is True


def test_candidate_table_uses_tuning_counts_but_final_cases_as_constraint(
    abstention: ModuleType,
) -> None:
    threshold = import_module("tests.it2.threshold")
    cases = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    trials = []
    for trial in range(1, 4):
        character = f"it2-abstention-t{trial}"
        queries = []
        for case in cases:
            session = case["input"]["sessions"][0]
            # Tuning positives are deliberately lost at 0.5; final positives remain.
            score = 0.8 if case["split"] == "final" and case["positive"] else 0.4
            queries.append(
                {
                    "case_id": case["id"],
                    "response": {
                        "memories": [],
                        "source_spans": [],
                        "verbatim_anchors": [] if case["positive"] else [{"id": "a"}],
                    },
                    "candidates": [
                        {
                            "id": case["id"],
                            "kind": "world",
                            "text": session["mentions"][0]["text"],
                            "reranker_score": score,
                            "sources": [
                                {
                                    "document_id": document_id(
                                        f"{character}-{session['session_id']}"
                                    ),
                                    "chunk_text": session["mentions"][0]["text"],
                                }
                            ],
                        }
                    ],
                }
            )
        trials.append(
            {
                "bank_id": f"bank-{trial}",
                "character_id": character,
                "completion": "complete",
                "queries": queries,
            }
        )
    table = threshold.evaluate_candidates(cases=cases, trials=trials, thresholds=[0.3, 0.5])
    assert [row["threshold"] for row in table] == [0.3, 0.5]
    low, high = table
    assert low["metrics"]["recall"] == 1.0
    assert low["metrics"]["abstention_rate"] == 0.0
    assert low["required_passed"] is False
    assert high["metrics"]["recall"] == 0.0
    assert high["metrics"]["abstention_rate"] == 1.0
    assert high["tuning_counts"]["positive_queries"] == 30
    assert high["required_passed"] is True
    assert high["metrics"]["anchor_contamination_rate"] == 1.0
    assert high["metrics"]["empty_fact_responses"] == 60
    assert high["metrics"]["empty_responses"] == 30
    assert len(high["trials"]) == 3
    assert all(row["metrics"]["recall"] == 0.0 for row in high["trials"])


def test_abstention_plan_sends_queries_without_expected_answers(abstention: ModuleType) -> None:
    cases: list[Case] = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    plan = abstention.build_abstention_plan(cases=cases, character_id="it2-abstention-t1")
    assert len(plan.recall_requests) == 40
    for case in cases:
        request = plan.recall_requests[case["id"]]
        assert request["query"] == case["input"]["query"]
        assert "expected" not in request
    assert len(plan.anchors) == 20


def test_timed_out_trials_are_recorded_once_each_without_recall_or_retry(
    abstention: ModuleType,
) -> None:
    cases = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    ingest = MagicMock()
    completion = MagicMock()
    completion.wait.return_value = CompletionResult("timeout", "completion wait limit exceeded")
    reader = MagicMock()
    recorder = MagicMock()
    result = abstention.run_abstention_trials(
        cases=cases,
        ingest=ingest,
        completion=completion,
        reader=reader,
        clock=lambda: 0.0,
        recorder=recorder,
    )
    rows = result["trials"]
    assert result["verdict"] == "FAIL"
    assert len(rows) == 3
    assert len({row["bank_id"] for row in rows}) == 3
    assert all(row["completion"] == "timeout" and row["metrics"] is None for row in rows)
    assert all(not any(row["cases"].values()) for row in rows)
    assert [call.args[0] for call in recorder.record.call_args_list] == rows
    assert completion.wait.call_count == 3
    assert ingest.ingest.call_count == 3
    ingest.recall.assert_not_called()
    assert reader.mock_calls == []


def test_one_service_failure_is_preserved_across_three_completed_trials(
    abstention: ModuleType, tmp_path: Path
) -> None:
    from tests.it2.quality_run import JsonlTrialRecorder

    cases = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    failed_case = next(case for case in cases if case["split"] == "final" and case["positive"])
    completion, reader = MagicMock(), MagicMock()
    completion.wait.return_value = CompletionResult("complete", "synthetic")
    reader.candidates.return_value = []

    class Port:
        def ingest(self, plan: object) -> None:
            pass

        def recall(self, character: str, request: dict[str, object]) -> RecallData:
            case = next(case for case in cases if case["input"]["query"] == request["query"])
            returned = case["positive"] and not (
                character.endswith("t1") and case["id"] == failed_case["id"]
            )
            return {
                "memories": [
                    {
                        "id": "f",
                        "source_turn_ids": [
                            f"{character}-{case['input']['sessions'][0]['mentions'][0]['id']}"
                        ],
                    }
                ]
                if returned
                else [],
                "source_spans": [],
            }

    path = tmp_path / "trials.jsonl"
    result = abstention.run_abstention_trials(
        cases=cases,
        ingest=Port(),
        completion=completion,
        reader=reader,
        clock=lambda: 0.0,
        recorder=JsonlTrialRecorder(path),
    )
    assert result["verdict"] == "FAIL"
    assert result["trials"][0]["cases"][failed_case["id"]] is False
    assert all(row["cases"][failed_case["id"]] for row in result["trials"][1:])
    assert [json.loads(line) for line in path.read_text().splitlines()] == result["trials"]
    assert reader.candidates.call_count == 120


def test_http_abstention_port_registers_approved_anchors_after_turns(
    abstention: ModuleType,
) -> None:
    from digital_souls_memory_hindsight.anchor_http import AnchorRequest

    cases = [case for case in load_cases(CORPUS) if case["acceptance_id"] == "AC-R4"]
    plan = abstention.build_abstention_plan(cases=cases, character_id="it2-abstention-t1")
    turns: set[str] = set()
    anchors: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/turns":
            turns.add(body["turn_id"])
        elif request.url.path.endswith("/verbatim-anchors"):
            anchor = AnchorRequest.model_validate(body)
            assert anchor.turn_id in turns
            assert anchor.approved_by == "IT2"
            anchors.append(anchor.anchor_id)
        return httpx.Response(201, json={})

    with httpx.Client(base_url="http://service", transport=httpx.MockTransport(respond)) as client:
        abstention.HttpAbstentionPort(client).ingest(plan)
    assert len(turns) == 40
    assert len(anchors) == 20


def test_candidate_reader_bridges_the_async_sdk(abstention: ModuleType) -> None:
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(return_value=_sdk_response())
    with asyncio.Runner() as runner:
        rows = abstention.HindsightCandidateReader(client, runner).candidates("bank", "合成 query")
    assert rows[0]["reranker_score"] == 0.5
    client.arecall.assert_awaited_once()
