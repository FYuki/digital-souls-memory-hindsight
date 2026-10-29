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
