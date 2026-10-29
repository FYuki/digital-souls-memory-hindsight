"""The official SDK adapter preserves fact identity and source chunks."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ApiException, NotFoundException
from hindsight_client_api.models.recall_response import RecallResponse

pytestmark = pytest.mark.ut


def sdk_response() -> RecallResponse:
    return RecallResponse.model_validate(
        {
            "results": [
                {
                    "id": "world",
                    "type": "world",
                    "text": "架空の世界の事実。",
                    "document_id": "doc-a",
                    "chunk_id": "chunk-a",
                },
                {
                    "id": "experience",
                    "type": "experience",
                    "text": "架空の経験。",
                    "document_id": "doc-b",
                    "chunk_id": "chunk-b",
                },
                {
                    "id": "observation",
                    "type": "observation",
                    "text": "架空の統合した事実。",
                    "source_fact_ids": ["source-a"],
                },
            ],
            "chunks": {
                "chunk-a": {
                    "id": "chunk-a",
                    "text": "世界の原文。",
                    "chunk_index": 0,
                    "truncated": False,
                },
                "chunk-b": {
                    "id": "chunk-b",
                    "text": "経験の原文。",
                    "chunk_index": 0,
                    "truncated": True,
                },
            },
            "source_facts": {
                "source-a": {
                    "id": "source-a",
                    "type": "world",
                    "text": "架空の世界の事実。",
                    "document_id": "doc-a",
                    "chunk_id": "chunk-a",
                },
            },
        }
    )


def test_adapter_requests_chunks_and_resolves_observation_sources() -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(return_value=sdk_response())
    facts = asyncio.run(module.HindsightRecall(client).recall("character-bank", "架空の検索"))
    client.arecall.assert_awaited_once()
    sent = client.arecall.await_args.kwargs
    assert sent["bank_id"] == "character-bank"
    assert sent["query"] == "架空の検索"
    assert sent["include_chunks"] is True
    assert sent["include_source_facts"] is True
    assert set(sent["types"]) == {"world", "experience", "observation"}
    assert [
        (fact.id, fact.kind, fact.text, fact.document_id, fact.chunk_text) for fact in facts
    ] == [
        ("world", "world", "架空の世界の事実。", "doc-a", "世界の原文。"),
        ("experience", "experience", "架空の経験。", "doc-b", "経験の原文。"),
        ("observation", "observation", "架空の統合した事実。", "doc-a", "世界の原文。"),
    ]


@pytest.mark.parametrize("chunks", [None, {}])
def test_absent_chunks_cannot_create_an_invented_source(chunks: object) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(
        return_value=RecallResponse.model_validate(
            {
                "results": [
                    {
                        "id": "fact",
                        "type": "world",
                        "text": "事実を原文に流用しない。",
                        "document_id": "doc",
                        "chunk_id": "missing",
                    }
                ],
                "chunks": chunks,
            }
        )
    )
    facts = asyncio.run(module.HindsightRecall(client).recall("bank", "query"))
    # An unresolved fact may be dropped here or by the service, but never gain a source.
    assert all(not fact.chunk_text for fact in facts)


@pytest.mark.parametrize(
    "error",
    [
        ApiException(status=503),
        aiohttp.ClientConnectionError("synthetic private detail"),
        TimeoutError("synthetic private detail"),
    ],
)
def test_sdk_and_transport_errors_become_recall_unavailable(error: Exception) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(side_effect=error)
    with pytest.raises(ports.RecallUnavailable):
        asyncio.run(module.HindsightRecall(client).recall("bank", "query"))


def test_missing_bank_is_an_empty_result() -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(side_effect=NotFoundException(status=404))
    assert asyncio.run(module.HindsightRecall(client).recall("bank", "query")) == ()


@pytest.mark.parametrize("kind", ["world", "experience", "observation"])
@pytest.mark.parametrize("score", [0.49, 0.5, 0.9, None])
def test_reranker_threshold_filters_each_result_kind(kind: str, score: float | None) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    payload = sdk_response().model_dump()
    result = next(item for item in payload["results"] if item["type"] == kind)
    result["scores"] = {"reranker": score, "final": 1.0, "semantic": 1.0}
    payload["results"] = [result]
    # The observation's own score decides; source fact scores must not override it.
    payload["source_facts"]["source-a"]["scores"] = {
        "reranker": 0.0 if score is not None and score >= 0.5 else 1.0,
        "final": 1.0,
    }
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(return_value=RecallResponse.model_validate(payload))
    facts = asyncio.run(
        module.HindsightRecall(client, min_reranker_score=0.5).recall("bank", "query")
    )
    assert [fact.id for fact in facts] == ([kind] if score is not None and score >= 0.5 else [])


def test_threshold_preserves_only_qualifying_candidates_in_response_order() -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    payload = sdk_response().model_dump()
    for result, score in zip(payload["results"], [0.49, 0.5, 0.9], strict=True):
        result["scores"] = {"reranker": score, "final": 1.0}
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(return_value=RecallResponse.model_validate(payload))
    facts = asyncio.run(
        module.HindsightRecall(client, min_reranker_score=0.5).recall("bank", "query")
    )
    assert [fact.id for fact in facts] == ["experience", "observation"]


@pytest.mark.parametrize("threshold", [None, 0.5])
def test_missing_scores_are_allowed_only_when_threshold_is_disabled(
    threshold: float | None,
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    client = MagicMock(spec=Hindsight)
    client.arecall = AsyncMock(return_value=sdk_response())
    facts = asyncio.run(
        module.HindsightRecall(client, min_reranker_score=threshold).recall("bank", "query")
    )
    assert [fact.id for fact in facts] == (
        ["world", "experience", "observation"] if threshold is None else []
    )
