"""Recall through HTTP, the real ledger, and the pinned fixture-backed Hindsight."""

import asyncio
import importlib
import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from hindsight_client import Hindsight
from hindsight_client_api.models.update_memory_request import UpdateMemoryRequest
from psycopg.rows import dict_row

from digital_souls_memory_hindsight.evidence.projection import bank_id
from tests.it1 import test_bank_projection as projection_tests
from tests.it1.conftest import ResponseGate
from tests.it1.test_bank_projection import (
    COFFEE,
    SECRET,
    TEA,
    Projection,
    assert_no_private_tokens,
    hindsight_client,
    wait_idle,
)

pytestmark = pytest.mark.it1
projection = projection_tests.projection
JAPANESE = "架空の花子は10月31日に東京タワーで土曜日の約束を守ります。"


@pytest.fixture
def consolidation_gate() -> Iterator[ResponseGate]:
    path = Path(os.environ["STUB_CONTROL_DIR"]) / ("consolidation-" + uuid4().hex)
    path.mkdir()
    gate = ResponseGate(path)
    try:
        yield gate
    finally:
        gate.release()
        asyncio.run(gate.wait_finished())
        shutil.rmtree(path)


async def recall(
    p: Projection,
    client: Hindsight,
    character: str,
    query_text: str,
    *,
    min_reranker_score: float | None = None,
    **changes: object,
) -> httpx.Response:
    adapter = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
    service = importlib.import_module("digital_souls_memory_hindsight.recall.service")
    http = importlib.import_module("digital_souls_memory_hindsight.recall_http")
    app = FastAPI()
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_provision")
    anchor_service = importlib.import_module("digital_souls_memory_hindsight.anchors.service")
    anchor_store = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    instance = service.RecallService(
        p.service,
        adapter.HindsightRecall(client, min_reranker_score=min_reranker_score),
        storage.PostgresProvisionStore(p.database),
        anchor_service.AnchorService(anchor_store.PostgresAnchorStore(p.database), p.service),
        clock=p.clock,
    )
    app.include_router(http.recall_router(lambda: instance))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    ) as transport:
        return await transport.post(
            f"/characters/{character}/recall",
            json={
                "session_id": p.session,
                "format": "assembled",
                "query": query_text,
                "sensitivity": "public",
                "classifier_version": "fixture-1",
                **changes,
            },
        )


async def wait_consolidated(client: Hindsight, bank: str) -> None:
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        operations = await client.operations.list_operations(
            bank_id=bank, type="consolidation", _request_timeout=5.0
        )
        assert all(item.status not in {"failed", "cancelled"} for item in operations.operations)
        if operations.operations and all(
            item.status == "completed" for item in operations.operations
        ):
            return
        assert asyncio.get_running_loop().time() < deadline, operations.to_dict()
        await asyncio.sleep(0.1)


def assert_sources(response: httpx.Response, expected: dict[str, str]) -> None:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["memories"], body
    spans = {span["turn_id"]: span for span in body["source_spans"]}
    assert spans, body
    for memory in body["memories"]:
        assert memory["source_turn_ids"], memory
        assert set(memory["source_turn_ids"]) <= spans.keys()
    for turn, span in spans.items():
        assert span["text"] == expected[turn]
        assert span["text"] == expected[turn][span["start"] : span["end"]]
    assert body["context"] == "\n".join(span["text"] for span in body["source_spans"])


def test_consolidated_recall_returns_ledger_sources_and_verbatim_japanese(
    projection: Projection,
) -> None:
    p = projection
    p.post_turn("japanese", text=JAPANESE)

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            await wait_consolidated(client, bank_id(p.characters[0]))
            for query in ("東京タワー", "10月31日", "土曜日の約束"):
                response = await recall(p, client, p.characters[0], query)
                assert_sources(response, {"japanese": JAPANESE})
                assert any(span["text"] == JAPANESE for span in response.json()["source_spans"])
                assert any(
                    memory["kind"] == "observation" for memory in response.json()["memories"]
                )

    asyncio.run(exercise())


def test_recall_isolates_speaker_other_character_and_partial_recipient(
    projection: Projection,
) -> None:
    p = projection
    a, b, c = p.characters
    p.post_turn("spoken", text=TEA + "\n" + SECRET, perceived_by=[a, c])
    p.receipt("spoken", len(TEA), len(TEA + "\n" + SECRET))
    p.post_turn("other", actor=b, text=COFFEE, perceived_by=[b])

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            for character in p.characters:
                await wait_consolidated(client, bank_id(character))
            query = "架空の花子、太郎、お茶、珈琲、金庫について"
            speaker = await recall(p, client, a, query)
            assert_sources(speaker, {"spoken": TEA + "\n" + SECRET})
            assert any(SECRET in span["text"] for span in speaker.json()["source_spans"])
            other = await recall(p, client, b, query)
            assert_sources(other, {"other": COFFEE})
            partial = await recall(p, client, c, query)
            assert_sources(partial, {"spoken": TEA})
            for field in ("source_spans", "memories", "context"):
                assert_no_private_tokens(other.json()[field], ("翡翠茶", "金庫", "七九三"))
                assert_no_private_tokens(
                    partial.json()[field], ("金庫", "暗証番号", "七九三", "琥珀珈琲")
                )

    asyncio.run(exercise())


def test_new_facts_are_recalled_while_consolidation_remains_blocked(
    projection: Projection,
    consolidation_gate: ResponseGate,
) -> None:
    p = projection
    p.post_turn("fresh", text=TEA + "\n" + consolidation_gate.marker)

    async def exercise() -> None:
        async with hindsight_client() as client:
            bank = bank_id(p.characters[0])
            try:
                await p.drain(client)
                await consolidation_gate.wait_entered()
                operations = await client.operations.list_operations(
                    bank_id=bank, type="consolidation", _request_timeout=5.0
                )
                assert operations.operations, operations.to_dict()
                assert any(
                    item.status in {"pending", "processing"} for item in operations.operations
                )
                assert all(item.status != "completed" for item in operations.operations)
                response = await recall(p, client, p.characters[0], "翡翠茶")
                assert_sources(response, {"fresh": TEA + "\n" + consolidation_gate.marker})
                assert any(TEA in memory["content"] for memory in response.json()["memories"])
            finally:
                consolidation_gate.release()
                await consolidation_gate.wait_finished()
                await wait_idle(client, bank)

    asyncio.run(exercise())


def test_recall_http_errors_use_the_public_contract(projection: Projection) -> None:
    async def exercise() -> None:
        async with hindsight_client() as client:
            missing = await recall(
                projection, client, projection.characters[0], "翡翠茶", session_id="missing"
            )
            assert missing.status_code == 404
            assert missing.json() == {"detail": "Recall source not found"}
            invalid = await recall(
                projection, client, projection.characters[0], "翡翠茶", turn_id="missing"
            )
            assert invalid.status_code == 422
            empty = await recall(projection, client, projection.characters[0], "翡翠茶")
            assert empty.status_code == 200, empty.text
            assert empty.json()["memories"] == empty.json()["source_spans"] == []
            assert empty.json()["context"] == ""

    asyncio.run(exercise())


@pytest.mark.parametrize("use_turn", [False, True])
def test_recall_persists_response_and_duplicate_keeps_original_row(
    projection: Projection, use_turn: bool
) -> None:
    p = projection
    p.post_turn("provision-source", text=JAPANESE)

    async def exercise() -> httpx.Response:
        async with hindsight_client() as client:
            await p.drain(client)
            return await recall(
                p,
                client,
                p.characters[0],
                "東京タワー",
                query=None if use_turn else "東京タワー",
                turn_id="provision-source" if use_turn else None,
            )

    response = asyncio.run(exercise())
    assert_sources(response, {"provision-source": JAPANESE})
    body = response.json()
    with psycopg.connect(p.database, row_factory=dict_row, connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT * FROM provision_records WHERE provision_id = %s", (body["provision_id"],)
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "turn"
    assert (row["character_id"], row["session_id"], row["surface"]) == (
        p.characters[0],
        p.session,
        "Web",
    )
    assert row["query"] == (None if use_turn else "東京タワー")
    assert row["turn_id"] == ("provision-source" if use_turn else None)
    assert row["sensitivity"] == (None if use_turn else "public")
    assert row["classifier_version"] == (None if use_turn else "fixture-1")
    assert row["memory_ids"] == [memory["id"] for memory in body["memories"]]
    assert row["source_spans"] == [
        {key: span[key] for key in ("turn_id", "start", "end")} for span in body["source_spans"]
    ]
    assert row["verbatim_anchor_ids"] == []
    assert row["temperament_version"] is row["personality_version"] is None
    assert row["created_at"] == p.clock.now

    ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_provision")
    record = ports.ProvisionRecord(
        **{
            **row,
            "memory_ids": tuple(row["memory_ids"]),
            "source_spans": tuple(ports.ProvisionSpan(**span) for span in row["source_spans"]),
            "verbatim_anchor_ids": tuple(row["verbatim_anchor_ids"]),
            "memory_trust": tuple(row["memory_trust"]),
            "source_span_trust": tuple(row["source_span_trust"]),
            "verbatim_anchor_trust": tuple(row["verbatim_anchor_trust"]),
        }
    )
    for duplicate in (
        record,
        replace(record, memory_ids=("replacement-forbidden",), memory_trust=("inferred",)),
    ):
        with pytest.raises(ports.ProvisionConflict):
            storage.PostgresProvisionStore(p.database).add(duplicate)
    with psycopg.connect(p.database, row_factory=dict_row, connect_timeout=5) as connection:
        after = connection.execute(
            "SELECT * FROM provision_records WHERE provision_id = %s", (body["provision_id"],)
        ).fetchall()
    assert after == rows


@pytest.mark.parametrize("format", ["assembled", "materials"])
def test_reranker_abstains_on_unrelated_query_and_persists_empty_provision(
    projection: Projection, format: str
) -> None:
    p = projection
    p.post_turn("tea", text=TEA)
    p.post_turn("japanese", text=JAPANESE)
    settings = importlib.import_module("digital_souls_memory_hindsight.settings")
    threshold = settings.load_settings().recall_min_reranker_score

    async def exercise() -> str:
        async with hindsight_client() as client:
            await p.drain(client)
            bank = bank_id(p.characters[0])
            await wait_consolidated(client, bank)
            raw = await client.arecall(
                bank_id=bank, query="コーヒー", types=["world", "experience", "observation"]
            )
            assert raw.results, "The unfiltered bank must return unrelated candidates"
            assert all(
                result.scores is not None and result.scores.reranker is not None
                for result in raw.results
            ), raw.to_dict()
            unfiltered = await recall(p, client, p.characters[0], "コーヒー", format=format)
            assert unfiltered.status_code == 200, unfiltered.text
            assert unfiltered.json()["memories"], unfiltered.text
            filtered = await recall(
                p,
                client,
                p.characters[0],
                "コーヒー",
                min_reranker_score=threshold,
                format=format,
            )
            assert filtered.status_code == 200, filtered.text
            body = filtered.json()
            assert body["memories"] == body["source_spans"] == body["verbatim_anchors"] == []
            assert body["context"] == ("" if format == "assembled" else None)
            for query in ("翡翠茶", "東京タワー"):
                related = await recall(
                    p, client, p.characters[0], query, min_reranker_score=threshold
                )
                assert_sources(related, {"tea": TEA, "japanese": JAPANESE})
                assert any(
                    memory["kind"] == "observation" for memory in related.json()["memories"]
                ), related.text
            return str(body["provision_id"])

    provision_id = asyncio.run(exercise())
    with psycopg.connect(p.database, row_factory=dict_row, connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT memory_ids, source_spans, memory_trust, source_span_trust, "
            "verbatim_anchor_trust FROM provision_records WHERE provision_id = %s",
            (provision_id,),
        ).fetchall()
    assert rows == [
        {
            "memory_ids": [],
            "source_spans": [],
            "memory_trust": [],
            "source_span_trust": [],
            "verbatim_anchor_trust": [],
        }
    ]
    records = [
        json.loads(line) for line in Path(os.environ["STUB_REQUEST_LOG"]).read_text().splitlines()
    ]
    assert any(
        record["path"] == "/v1/rerank" and record["req"]["query"] == "コーヒー"
        for record in records
    )


def test_reranker_scores_support_outer_and_server_thresholds(projection: Projection) -> None:
    p = projection
    experience = "架空の太郎は翡翠茶の試飲を経験しました。"
    p.post_turn("tea", text=TEA)
    p.post_turn("japanese", text=JAPANESE)
    p.post_turn("experience", text=experience)
    settings = importlib.import_module("digital_souls_memory_hindsight.settings")
    threshold = settings.load_settings().recall_min_reranker_score

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            bank = bank_id(p.characters[0])
            await wait_consolidated(client, bank)
            initial = await client.arecall(bank_id=bank, query=experience, types=["world"])
            fact = next(item for item in initial.results if experience in item.text)
            # Use the official curation API to prepare an experience fact without
            # changing the extraction stub or Hindsight's implementation.
            await client.memory.update_memory(
                bank_id=bank,
                memory_id=fact.id,
                update_memory_request=UpdateMemoryRequest(text=fact.text, fact_type="experience"),
                _request_timeout=10.0,
            )
            await wait_idle(client, bank)
            seen_types: set[str | None] = set()
            for query in ("コーヒー", "翡翠茶", "東京タワー", experience):
                raw = await client.arecall(
                    bank_id=bank, query=query, types=["world", "experience", "observation"]
                )
                assert raw.results
                seen_types.update(item.type for item in raw.results)
                assert all(
                    item.scores is not None and item.scores.reranker is not None
                    for item in raw.results
                ), raw.to_dict()
                server = await client.arecall(
                    bank_id=bank,
                    query=query,
                    types=["world", "experience", "observation"],
                    min_scores={"reranker": threshold},
                )
                expected = {
                    item.id
                    for item in raw.results
                    if item.scores is not None
                    and item.scores.reranker is not None
                    and item.scores.reranker >= threshold
                }
                assert {item.id for item in server.results} == expected
                if query == "コーヒー":
                    assert server.results == []
                else:
                    assert server.results
                print(
                    json.dumps(
                        {
                            "query": query,
                            "threshold": threshold,
                            "scores": [
                                {"type": item.type, "reranker": item.scores.reranker}
                                for item in raw.results
                                if item.scores is not None
                            ],
                            "server_results": len(server.results),
                        },
                        ensure_ascii=False,
                    )
                )
            assert seen_types == {"world", "experience", "observation"}

    asyncio.run(exercise())
