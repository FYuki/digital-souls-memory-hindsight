"""Recall contracts at HTTP, with the real evidence service and isolated I/O."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.projection import bank_id, document_id
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from tests.ut.test_delivery_scope import NOW, Clock
from tests.ut.test_evidence_http import MemoryStore, receipt_body, session_body, turn_body

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.recall.ports import ProvisionRecord

pytestmark = pytest.mark.ut
TEA = "架空の花子は翡翠茶を飲みます。"
SECRET = "架空の金庫の暗証番号は七九三です。"
MOON = "架空の月には青銅塔があります。"


def recall_body(**changes: object) -> dict[str, object]:
    return {
        "session_id": "s-1",
        "format": "materials",
        "query": "翡翠茶",
        "sensitivity": "public",
        "classifier_version": "fixture-1",
        **changes,
    }


@dataclass
class MemoryRecall:
    facts: tuple[object, ...] = ()
    error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def recall(self, bank_id: str, query: str) -> tuple[object, ...]:
        self.calls.append((bank_id, query))
        if self.error is not None:
            raise self.error
        return self.facts


@dataclass
class ProvisionStore:
    records: dict[str, ProvisionRecord] = field(default_factory=dict)
    error: Exception | None = None

    def add(self, value: ProvisionRecord) -> None:
        if self.error is not None:
            raise self.error
        if value.provision_id in self.records:
            ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
            raise ports.ProvisionConflict()
        self.records[value.provision_id] = value


@dataclass
class RecallHTTP:
    app: FastAPI
    memory: MemoryRecall
    provisions: ProvisionStore

    def turn(self, client: TestClient, **changes: object) -> None:
        response = client.post("/turns", json=turn_body(**{"text": TEA, **changes}))
        assert response.status_code == 201, response.text

    def fact(self, identifier: str, **changes: object) -> object:
        ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
        return ports.RecalledFact(
            **{
                "id": identifier,
                "kind": "world",
                "text": TEA,
                "document_id": document_id("s-1"),
                "chunk_text": TEA,
                **changes,
            }
        )


def application(
    *,
    store: MemoryStore | None = None,
    characters: frozenset[str] | None = frozenset({"char-a", "char-b", "char-c"}),
    configured: bool = True,
) -> RecallHTTP:
    service_module = importlib.import_module("digital_souls_memory_hindsight.recall.service")
    http_module = importlib.import_module("digital_souls_memory_hindsight.recall_http")
    store = store if store is not None else MemoryStore()
    evidence = EvidenceService(store, characters)
    memory = MemoryRecall()
    provisions = ProvisionStore()
    service = service_module.RecallService(
        evidence, memory if configured else None, provisions, clock=Clock()
    )
    app = FastAPI()
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(http_module.recall_router(lambda: service))
    return RecallHTTP(app, memory, provisions)


def start(client: TestClient, **changes: object) -> None:
    response = client.post("/sessions", json=session_body(**changes))
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("changes", "omitted"),
    [
        ({"extra": "forbidden"}, None),
        ({}, "session_id"),
        ({"session_id": ""}, None),
        ({"session_id": " \t "}, None),
        ({"session_id": 1}, None),
        ({"format": "unknown"}, None),
        ({"query": "翡翠茶", "turn_id": "t-1"}, None),
        ({}, "query"),
        ({}, "sensitivity"),
        ({}, "classifier_version"),
        ({"sensitivity": None}, None),
        ({"classifier_version": None}, None),
        ({"query": ["翡翠茶"]}, None),
    ],
)
def test_invalid_recall_requests_are_422_without_search(
    changes: dict[str, object], omitted: str | None
) -> None:
    h = application()
    body = recall_body(**changes)
    if omitted is not None:
        del body[omitted]
    with TestClient(h.app) as client:
        start(client)
        response = client.post("/characters/char-a/recall", json=body)
    assert response.status_code == 422
    assert h.memory.calls == []
    assert h.provisions.records == {}


def test_query_strings_are_stripped_before_lookup_and_search() -> None:
    h = application()
    with TestClient(h.app) as client:
        start(client)
        response = client.post(
            "/characters/char-a/recall",
            json=recall_body(
                session_id="  s-1 \t",
                format=" materials ",
                query=" 翡翠茶 \n",
                sensitivity=" public ",
                classifier_version=" fixture-1 ",
            ),
        )
    assert response.status_code == 200, response.text
    assert h.memory.calls == [(bank_id("char-a"), "翡翠茶")]


def test_turn_query_uses_only_delivered_text_without_classifier_fields() -> None:
    h = application()
    with TestClient(h.app) as client:
        start(client)
        h.turn(client, perceived_by=["char-a", "char-c"], text=TEA + SECRET)
        assert (
            client.post(
                "/turns/t-1/receipts", json=receipt_body(status="partial", spoken_until=len(TEA))
            ).status_code
            == 201
        )
        response = client.post(
            "/characters/char-c/recall",
            json={"session_id": "s-1", "format": "materials", "turn_id": " t-1 "},
        )
    assert response.status_code == 200, response.text
    assert h.memory.calls == [(bank_id("char-c"), TEA)]


@pytest.mark.parametrize("format", ["materials", "assembled"])
def test_response_matches_public_keys_types_and_japanese_json(format: str) -> None:
    h = application()
    h.memory.facts = (h.fact("fact-tea"),)
    with TestClient(h.app) as client:
        start(client)
        h.turn(client)
        response = client.post("/characters/char-a/recall", json=recall_body(format=format))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "provision_id",
        "temperament_version",
        "personality_version",
        "memories",
        "source_spans",
        "verbatim_anchors",
        "context",
    }
    assert isinstance(body["provision_id"], str)
    UUID(body["provision_id"])
    assert body["temperament_version"] is None
    assert body["personality_version"] is None
    assert body["verbatim_anchors"] == []
    assert body["memories"] == [
        {"id": "fact-tea", "kind": "world", "content": TEA, "source_turn_ids": ["t-1"]}
    ]
    assert body["source_spans"] == [
        {
            "turn_id": "t-1",
            "start": 0,
            "end": len(TEA),
            "text": TEA,
            "actor": "char-a",
            "language": "ja",
            "truncated": False,
            "original_start": 0,
            "original_end": len(TEA),
        }
    ]
    span = body["source_spans"][0]
    for key in ("start", "end", "original_start", "original_end"):
        assert type(span[key]) is int
    assert type(span["truncated"]) is bool
    assert body["context"] == (TEA if format == "assembled" else None)
    assert TEA.encode() in response.content


def test_each_response_has_a_new_uuid() -> None:
    h = application()
    with TestClient(h.app) as client:
        start(client)
        first = client.post("/characters/char-a/recall", json=recall_body())
        second = client.post("/characters/char-a/recall", json=recall_body())
    assert first.status_code == second.status_code == 200
    assert UUID(first.json()["provision_id"]) != UUID(second.json()["provision_id"])
    assert set(h.provisions.records) == {
        first.json()["provision_id"],
        second.json()["provision_id"],
    }
    assert all(
        record.memory_ids == () and record.source_spans == ()
        for record in h.provisions.records.values()
    )


def test_chunk_overlap_maps_sources_and_preserves_fact_order() -> None:
    h = application()
    h.memory.facts = (
        h.fact(
            "z-first",
            kind="observation",
            text="お茶についての統合事実",
            chunk_text=TEA + "\n\n" + MOON,
        ),
        h.fact("a-second", kind="experience"),
        h.fact("unmapped", chunk_text="架空の砂漠の旅の話。"),
    )
    with TestClient(h.app) as client:
        start(client)
        h.turn(client)
        response = client.post("/characters/char-a/recall", json=recall_body(format="assembled"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["memories"] == [
        {
            "id": "z-first",
            "kind": "observation",
            "content": "お茶についての統合事実",
            "source_turn_ids": ["t-1"],
        },
        {"id": "a-second", "kind": "experience", "content": TEA, "source_turn_ids": ["t-1"]},
    ]
    assert [span["turn_id"] for span in body["source_spans"]] == ["t-1"]


def test_same_text_in_another_session_is_not_a_source() -> None:
    h = application()
    h.memory.facts = (h.fact("wrong-session"),)
    with TestClient(h.app) as client:
        start(client)
        start(client, session_id="s-2")
        h.turn(client, session_id="s-2")
        h.turn(client, turn_id="other", text=MOON)
        response = client.post("/characters/char-a/recall", json=recall_body())
    assert response.status_code == 200, response.text
    assert response.json()["memories"] == []
    assert response.json()["source_spans"] == []


def test_multiple_source_spans_are_joined_with_newlines_in_recall_order() -> None:
    h = application()
    h.memory.facts = (
        h.fact("moon", chunk_text=MOON, text=MOON),
        h.fact("tea"),
    )
    with TestClient(h.app) as client:
        start(client)
        h.turn(client)
        h.turn(client, turn_id="moon-turn", text=MOON)
        response = client.post("/characters/char-a/recall", json=recall_body(format="assembled"))
    assert response.status_code == 200, response.text
    assert response.json()["context"] == MOON + "\n" + TEA
    assert [span["turn_id"] for span in response.json()["source_spans"]] == ["moon-turn", "t-1"]


def test_spans_use_current_delivery_even_when_chunks_are_stale() -> None:
    h = application()
    h.memory.facts = (h.fact("tea", chunk_text=TEA + SECRET),)
    with TestClient(h.app) as client:
        start(client)
        h.turn(client, perceived_by=["char-a", "char-c"], text=TEA + SECRET)
        assert (
            client.post(
                "/turns/t-1/receipts",
                json=receipt_body(status="full", spoken_until=len(TEA + SECRET)),
            ).status_code
            == 201
        )
        before = client.post("/characters/char-c/recall", json=recall_body(format="assembled"))
        assert before.status_code == 200, before.text
        assert before.json()["source_spans"][0]["text"] == TEA + SECRET
        assert (
            client.post(
                "/turns/t-1/receipts", json=receipt_body(status="partial", spoken_until=len(TEA))
            ).status_code
            == 201
        )
        after = client.post("/characters/char-c/recall", json=recall_body(format="assembled"))
        assert after.status_code == 200, after.text
        span = after.json()["source_spans"][0]
        assert span["text"] == after.json()["context"] == TEA
        assert span["end"] == span["original_end"] == len(TEA)
        assert after.json()["memories"][0]["source_turn_ids"] == ["t-1"]
        assert client.post("/turns/t-1/receipts", json=receipt_body()).status_code == 201
        removed = client.post("/characters/char-c/recall", json=recall_body(format="assembled"))
    assert removed.status_code == 200, removed.text
    assert removed.json()["memories"] == removed.json()["source_spans"] == []
    assert removed.json()["context"] == ""


@pytest.mark.parametrize(
    "source", ["session", "character", "turn", "unperceived", "thought", "pending", "none"]
)
def test_missing_or_unperceived_sources_are_404_without_search(source: str) -> None:
    h = application()
    with TestClient(h.app) as client:
        start(client)
        h.turn(
            client,
            perceived_by=["char-a", "char-c"] if source in {"pending", "none"} else ["char-a"],
            kind="thought" if source == "thought" else "utterance",
        )
        if source == "none":
            assert client.post("/turns/t-1/receipts", json=receipt_body()).status_code == 201
        body = recall_body(session_id="absent" if source == "session" else "s-1")
        if source not in {"session", "character"}:
            body = {
                "session_id": "s-1",
                "format": "materials",
                "turn_id": "absent" if source == "turn" else "t-1",
            }
        character = "unknown" if source == "character" else "char-c"
        response = client.post(f"/characters/{character}/recall", json=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Recall source not found"}
    assert h.memory.calls == []
    assert h.provisions.records == {}


@pytest.mark.parametrize("failure", ["ledger", "characters", "memory", "search"])
def test_unavailable_dependencies_return_sanitized_503(failure: str) -> None:
    class UnavailableStore(MemoryStore):
        def get_session(self, session_id: str) -> None:
            raise EvidenceUnavailable("synthetic private connection detail")

    h = application(
        store=UnavailableStore() if failure == "ledger" else None,
        characters=None if failure == "characters" else frozenset({"char-a"}),
        configured=failure != "memory",
    )
    if failure == "search":
        ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
        h.memory.error = ports.RecallUnavailable("synthetic private model detail")
    with TestClient(h.app) as client:
        start(client)
        response = client.post("/characters/char-a/recall", json=recall_body())
    assert response.status_code == 503
    assert response.json() == {"detail": "Recall unavailable"}
    assert h.provisions.records == {}


def test_labels_do_not_change_search_destination_or_arguments() -> None:
    h = application()
    with TestClient(h.app) as client:
        start(client)
        for label in ("public", "private"):
            response = client.post("/characters/char-a/recall", json=recall_body(sensitivity=label))
            assert response.status_code == 200, response.text
    assert h.memory.calls == [(bank_id("char-a"), "翡翠茶")] * 2


def test_production_app_exposes_recall_and_unconfigured_memory_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    module = importlib.reload(importlib.import_module("digital_souls_memory_hindsight.app"))
    store = MemoryStore()
    monkeypatch.setattr(module, "ledger_service", EvidenceService(store, frozenset({"char-a"})))
    worker = MagicMock()
    worker.run = AsyncMock()
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=worker))

    async def exercise() -> None:
        async with module.app.router.lifespan_context(module.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=module.app), base_url="http://service"
            ) as client:
                assert (await client.post("/sessions", json=session_body())).status_code == 201
                response = await client.post("/characters/char-a/recall", json=recall_body())
                assert response.status_code == 503
                assert response.json() == {"detail": "Recall unavailable"}

    asyncio.run(exercise())


@pytest.mark.parametrize("format", ["materials", "assembled"])
def test_query_provision_saves_response_order_metadata_and_service_time(format: str) -> None:
    h = application()
    h.memory.facts = (
        h.fact("z-moon", chunk_text=MOON, text=MOON),
        h.fact("a-tea"),
        h.fact("excluded", chunk_text="架空の砂漠の旅の話。"),
    )
    with TestClient(h.app) as client:
        start(client, surface="SyntheticSurface")
        h.turn(client)
        h.turn(client, turn_id="moon-turn", text=MOON)
        response = client.post(
            "/characters/char-a/recall",
            json=recall_body(
                format=format,
                query=" 翡翠茶 ",
                sensitivity=" private ",
                classifier_version=" classifier-2 ",
            ),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(h.provisions.records) == 1
    record = h.provisions.records[body["provision_id"]]
    assert (record.kind, record.character_id, record.session_id, record.surface) == (
        "turn",
        "char-a",
        "s-1",
        "SyntheticSurface",
    )
    assert (record.query, record.turn_id, record.sensitivity, record.classifier_version) == (
        "翡翠茶",
        None,
        "private",
        "classifier-2",
    )
    assert record.memory_ids == tuple(memory["id"] for memory in body["memories"])
    assert record.memory_ids == ("z-moon", "a-tea")
    assert [asdict(span) for span in record.source_spans] == [
        {key: span[key] for key in ("turn_id", "start", "end")} for span in body["source_spans"]
    ]
    assert len(record.source_spans) == 2
    assert record.verbatim_anchor_ids == ()
    assert record.temperament_version is record.personality_version is None
    assert record.created_at == NOW
    assert h.memory.calls == [(bank_id("char-a"), "翡翠茶")]


@pytest.mark.parametrize("with_classification", [False, True])
def test_turn_provision_saves_turn_id_without_query_or_classification(
    with_classification: bool,
) -> None:
    h = application()
    h.memory.facts = (h.fact("tea"),)
    body: dict[str, object] = {"session_id": "s-1", "format": "materials", "turn_id": " t-1 "}
    if with_classification:
        body.update(sensitivity="private", classifier_version="ignored-classifier")
    with TestClient(h.app) as client:
        start(client)
        h.turn(client)
        response = client.post("/characters/char-a/recall", json=body)
    assert response.status_code == 200, response.text
    assert len(h.provisions.records) == 1
    record = h.provisions.records[response.json()["provision_id"]]
    assert record.turn_id == "t-1"
    assert record.query is record.sensitivity is record.classifier_version is None
    assert record.memory_ids == ("tea",)
    assert [asdict(span) for span in record.source_spans] == [
        {"turn_id": "t-1", "start": 0, "end": len(TEA)}
    ]


def test_save_failure_returns_only_503_and_next_request_can_save() -> None:
    h = application()
    h.provisions.error = EvidenceUnavailable("synthetic private storage detail")
    with TestClient(h.app) as client:
        start(client)
        failed = client.post("/characters/char-a/recall", json=recall_body())
        assert failed.status_code == 503
        assert failed.json() == {"detail": "Recall unavailable"}
        assert h.provisions.records == {}
        h.provisions.error = None
        success = client.post("/characters/char-a/recall", json=recall_body())
    assert success.status_code == 200, success.text
    assert set(h.provisions.records) == {success.json()["provision_id"]}


def test_duplicate_provision_returns_409_without_overwriting_first_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.recall.service")
    monkeypatch.setattr(module, "uuid4", lambda: UUID(int=1))
    h = application()
    with TestClient(h.app) as client:
        start(client)
        first = client.post("/characters/char-a/recall", json=recall_body())
        assert first.status_code == 200, first.text
        original = h.provisions.records[first.json()["provision_id"]]
        duplicate = client.post("/characters/char-a/recall", json=recall_body(query="別の検索"))
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Provision already exists"}
    assert list(h.provisions.records.values()) == [original]
    assert original.query == "翡翠茶"


def test_recall_router_exposes_only_the_existing_post() -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.recall_http")
    app = FastAPI()
    app.include_router(module.recall_router(lambda: None))
    paths = app.openapi()["paths"]
    assert set(paths) == {"/characters/{character_id}/recall"}
    assert set(paths["/characters/{character_id}/recall"]) == {"post"}
