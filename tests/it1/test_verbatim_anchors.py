"""Approved ledger anchors survive recall abstention without leaking perception."""

import asyncio
import importlib

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import Projection, hindsight_client
from tests.it1.test_recall_api import recall

pytestmark = pytest.mark.it1
projection = projection_tests.projection
PROMISE = "架空の花子は土曜日の約束で東京タワーへ行きます。"
ANNIVERSARY = "架空の二人の記念日は十月三十一日です。"


def anchors(p: Projection) -> TestClient:
    service = importlib.import_module("digital_souls_memory_hindsight.anchors.service")
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    http = importlib.import_module("digital_souls_memory_hindsight.anchor_http")
    instance = service.AnchorService(
        storage.PostgresAnchorStore(p.database), p.service, clock=p.clock
    )
    app = FastAPI()
    app.include_router(http.anchor_router(lambda: instance))
    return TestClient(app)


def register(
    client: TestClient, character: str, anchor: str, turn: str, kind: str
) -> httpx.Response:
    return client.post(
        f"/characters/{character}/verbatim-anchors",
        json={
            "anchor_id": anchor,
            "turn_id": turn,
            "kind": kind,
            "reason": "架空の重要な原文",
            "approved_by": "synthetic-author",
        },
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("土曜日の約束", {"promise"}),
        ("記念日", {"anniversary"}),
        # Natural questions contain words absent from the anchor text.
        ("土曜日の約束はいつ？", {"promise"}),
        ("二人の記念日を覚えてる？", {"anniversary"}),
        ("今日はいい天気ですね", set()),
        ("いつ？", set()),
    ],
)
def test_ledger_full_text_search_matches_japanese_questions(
    database: str, query: str, expected: set[str]
) -> None:
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    candidates = (("promise", PROMISE), ("anniversary", ANNIVERSARY))
    assert storage.PostgresAnchorStore(database).match(candidates, query) == expected


@pytest.mark.parametrize(
    ("kind", "text", "query"),
    [
        ("commitment", PROMISE, "土曜日の約束"),
        ("anniversary", ANNIVERSARY, "記念日"),
        ("commitment", PROMISE, "土曜日の約束はいつ？"),
    ],
)
def test_japanese_anchor_survives_reranker_abstention_and_is_recorded(
    projection: Projection, kind: str, text: str, query: str
) -> None:
    p = projection
    character = p.characters[0]
    p.post_turn("unrelated", text=f"架空の{query[:2]}に関する別の話題です。")

    async def prepare_bank() -> None:
        async with hindsight_client() as client:
            await p.drain(client)

    asyncio.run(prepare_bank())
    # Leave this new source unprojected: Hindsight has only unrelated facts.
    p.post_turn("anchor-source", text=text)
    with anchors(p) as client:
        created = register(client, character, "anchor-1", "anchor-source", kind)
        assert created.status_code == 201, created.text
        assert created.json()["text"] == text
        assert (
            client.get(f"/characters/{character}/verbatim-anchors/anchor-1").json()
            == created.json()
        )
        assert client.get(f"/characters/{character}/verbatim-anchors").json() == [created.json()]
        duplicate = register(client, character, "anchor-1", "anchor-source", kind)
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": "Voice already exists"}

    async def exercise() -> httpx.Response:
        async with hindsight_client() as client:
            unfiltered = await recall(p, client, character, query)
            assert unfiltered.status_code == 200, unfiltered.text
            assert unfiltered.json()["memories"], unfiltered.text
            return await recall(p, client, character, query, min_reranker_score=0.9)

    response = asyncio.run(exercise())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["memories"] == []
    assert body["verbatim_anchors"] == [
        {"anchor_id": "anchor-1", "turn_id": "anchor-source", "text": text, "trust": "evidence"}
    ]
    assert [(s["turn_id"], s["start"], s["end"], s["text"]) for s in body["source_spans"]] == [
        ("anchor-source", 0, len(text), text)
    ]
    assert body["context"] == text
    with psycopg.connect(p.database, row_factory=dict_row, connect_timeout=5) as connection:
        record = connection.execute(
            "SELECT verbatim_anchor_ids, source_spans FROM provision_records WHERE provision_id=%s",
            (body["provision_id"],),
        ).fetchone()
    assert record == {
        "verbatim_anchor_ids": ["anchor-1"],
        "source_spans": [{"turn_id": "anchor-source", "start": 0, "end": len(text)}],
    }


def test_full_text_search_uses_delivered_prefix_and_isolates_other_characters(
    projection: Projection,
) -> None:
    p = projection
    a, b, c = p.characters
    p.post_turn("partial-source", text=PROMISE + ANNIVERSARY, perceived_by=[a, b])
    p.receipt("partial-source", len(PROMISE), len(PROMISE + ANNIVERSARY))
    with anchors(p) as client:
        assert register(client, b, "partial", "partial-source", "commitment").status_code == 201
        denied = register(client, c, "hidden", "partial-source", "anniversary")
        assert denied.status_code == 404
        assert denied.json() == {"detail": "Voice not found"}

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            allowed = await recall(p, client, b, "土曜日の約束", min_reranker_score=1.0)
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["verbatim_anchors"] == [
                {
                    "anchor_id": "partial",
                    "turn_id": "partial-source",
                    "text": PROMISE,
                    "trust": "evidence",
                }
            ]
            for character, query in ((b, "記念日"), (c, "土曜日の約束")):
                denied = await recall(p, client, character, query, min_reranker_score=1.0)
                assert denied.status_code == 200, denied.text
                body = denied.json()
                assert body["verbatim_anchors"] == body["source_spans"] == body["memories"] == []
                assert body["context"] == ""

    asyncio.run(exercise())


@pytest.mark.parametrize("query", ["土曜日の約束 OR (", "架空の' %s"])
def test_full_text_query_syntax_does_not_make_recall_unavailable(
    projection: Projection, query: str
) -> None:
    p = projection
    p.post_turn("syntax-source", text=PROMISE)
    with anchors(p) as client:
        assert (
            register(client, p.characters[0], "syntax", "syntax-source", "commitment").status_code
            == 201
        )

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            response = await recall(p, client, p.characters[0], query, min_reranker_score=1.0)
            assert response.status_code == 200, response.text

    asyncio.run(exercise())


@pytest.mark.parametrize("query", ['"', "-"])
def test_full_text_syntax_characters_return_anchor_id_set(
    projection: Projection, query: str
) -> None:
    p = projection
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    matched = storage.PostgresAnchorStore(p.database).match((("syntax", PROMISE),), query)
    assert isinstance(matched, frozenset)
    assert matched <= {"syntax"}
