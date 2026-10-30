"""Anchor HTTP contracts with the real ledger and an isolated anchor store."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from tests.ut.test_delivery_scope import NOW, Clock
from tests.ut.test_evidence_http import MemoryStore, receipt_body, session_body, turn_body

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.anchors.ports import VerbatimAnchor

pytestmark = pytest.mark.ut
BASE = "/characters/char-a/verbatim-anchors"
TEXT = "  架空の土曜日の約束🍵。\n"


def anchor_body(**changes: object) -> dict[str, object]:
    return {
        "anchor_id": "anchor-1",
        "turn_id": "t-1",
        "kind": "commitment",
        "reason": " 架空の約束 \n",
        "approved_by": " synthetic-author \t",
        **changes,
    }


@dataclass
class AnchorStore:
    values: dict[tuple[str, str], VerbatimAnchor] = field(default_factory=dict)
    error: Exception | None = None
    matches: list[tuple[tuple[tuple[str, str], ...], str]] = field(default_factory=list)

    def add(self, value: VerbatimAnchor) -> None:
        if self.error is not None:
            raise self.error
        key = (value.character_id, value.anchor_id)
        if key in self.values:
            ports = importlib.import_module("digital_souls_memory_hindsight.anchors.ports")
            raise ports.AnchorConflict()
        self.values[key] = value

    def get(self, character_id: str, anchor_id: str) -> VerbatimAnchor | None:
        if self.error is not None:
            raise self.error
        return self.values.get((character_id, anchor_id))

    def list(self, character_id: str) -> tuple[VerbatimAnchor, ...]:
        if self.error is not None:
            raise self.error
        return tuple(
            sorted(
                (
                    value
                    for (character, _), value in self.values.items()
                    if character == character_id
                ),
                key=lambda value: (value.registered_at, value.anchor_id),
            )
        )

    def match(self, candidates: tuple[tuple[str, str], ...], query: str) -> frozenset[str]:
        if self.error is not None:
            raise self.error
        self.matches.append((candidates, query))
        # This fake proves the handoff of projected text, not PGroonga semantics.
        return frozenset(identifier for identifier, text in candidates if query in text)


def application(
    store: AnchorStore,
    characters: frozenset[str] | None = frozenset({"char-a", "char-b", "char-c"}),
) -> FastAPI:
    service = importlib.import_module("digital_souls_memory_hindsight.anchors.service")
    http = importlib.import_module("digital_souls_memory_hindsight.anchor_http")
    evidence = EvidenceService(MemoryStore(), characters, clock=Clock())
    instance = service.AnchorService(store, evidence, clock=Clock())
    app = FastAPI()
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(http.anchor_router(lambda: instance))
    return app


def seed(client: TestClient, **changes: object) -> None:
    assert client.post("/sessions", json=session_body()).status_code == 201
    response = client.post("/turns", json=turn_body(**{"text": TEXT, **changes}))
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("kind", ["identity_statement", "commitment", "nickname", "anniversary"])
def test_register_get_and_list_preserve_verbatim_text_metadata_and_service_clock(kind: str) -> None:
    store = AnchorStore()
    with TestClient(application(store)) as client:
        seed(client)
        created = client.post(BASE, json=anchor_body(kind=kind))
        assert created.status_code == 201, created.text
        body = created.json()
        assert set(body) == {
            "character_id",
            "anchor_id",
            "turn_id",
            "kind",
            "reason",
            "approved_by",
            "registered_at",
            "text",
        }
        assert {key: body[key] for key in anchor_body()} == anchor_body(kind=kind)
        assert body["character_id"] == "char-a"
        assert body["text"] == TEXT
        assert datetime.fromisoformat(body["registered_at"]) == NOW
        assert client.get(BASE + "/anchor-1").json() == body
        assert client.get(BASE).json() == [body]


@pytest.mark.parametrize(
    ("changes", "omitted"),
    [
        ({"kind": "unknown"}, None),
        ({"kind": " commitment "}, None),
        ({"anchor_id": "bad.id"}, None),
        ({"anchor_id": " anchor-1 "}, None),
        ({"turn_id": "日本語"}, None),
        ({"turn_id": ""}, None),
        ({"reason": " \t\n"}, None),
        ({"approved_by": "\u3000\n"}, None),
        ({"approved_by": ""}, None),
        ({"reason": ""}, None),
        ({"text": "言い換え禁止"}, None),
        ({"extra": "forbidden"}, None),
        *[({key: 1}, None) for key in ("anchor_id", "turn_id", "kind", "reason", "approved_by")],
        *[({key: None}, None) for key in ("anchor_id", "turn_id", "kind", "reason", "approved_by")],
        *[({}, key) for key in ("anchor_id", "turn_id", "kind", "reason", "approved_by")],
    ],
)
def test_invalid_registration_is_422_without_writing(
    changes: dict[str, object], omitted: str | None
) -> None:
    store = AnchorStore()
    body = anchor_body(**changes)
    if omitted is not None:
        del body[omitted]
    with TestClient(application(store)) as client:
        seed(client)
        response = client.post(BASE, json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid voice value"}
    assert store.values == {}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/characters/bad.id/verbatim-anchors"),
        ("GET", "/characters/bad.id/verbatim-anchors"),
        ("GET", BASE + "/bad.id"),
        ("GET", "/characters/bad.id/verbatim-anchors/anchor-1"),
    ],
)
def test_invalid_path_identifiers_use_voice_validation_error(method: str, path: str) -> None:
    store = AnchorStore()
    with TestClient(application(store)) as client:
        response = client.request(method, path, json=anchor_body() if method == "POST" else None)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid voice value"}
    assert store.values == {}


def test_duplicate_preserves_first_registration_and_id_is_scoped_to_character() -> None:
    store = AnchorStore()
    with TestClient(application(store)) as client:
        seed(client, perceived_by=["char-a", "char-b"])
        assert (
            client.post(
                "/turns/t-1/receipts", json=receipt_body(status="full", spoken_until=len(TEXT))
            ).status_code
            == 201
        )
        first = client.post(BASE, json=anchor_body())
        assert first.status_code == 201, first.text
        duplicate = client.post(BASE, json=anchor_body(reason="架空の置換"))
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": "Voice already exists"}
        assert client.get(BASE + "/anchor-1").json() == first.json()
        other = client.post("/characters/char-b/verbatim-anchors", json=anchor_body())
        assert other.status_code == 201, other.text
        assert client.get(BASE).json() == [first.json()]
        assert client.get("/characters/char-b/verbatim-anchors").json() == [other.json()]


@pytest.mark.parametrize("source", ["missing", "unperceived", "pending", "none"])
def test_missing_or_unperceived_turn_is_404_without_writing(source: str) -> None:
    store = AnchorStore()
    with TestClient(application(store)) as client:
        seed(
            client,
            perceived_by=["char-a", "char-b"] if source in {"pending", "none"} else ["char-a"],
        )
        if source == "none":
            assert client.post("/turns/t-1/receipts", json=receipt_body()).status_code == 201
        response = client.post(
            "/characters/char-b/verbatim-anchors",
            json=anchor_body(turn_id="missing" if source == "missing" else "t-1"),
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "Voice not found"}
    assert store.values == {}


@pytest.mark.parametrize(("method", "suffix"), [("POST", ""), ("GET", ""), ("GET", "/anchor-1")])
def test_unregistered_character_or_missing_anchor_is_404(method: str, suffix: str) -> None:
    store = AnchorStore()
    with TestClient(application(store)) as client:
        seed(client)
        response = client.request(
            method,
            "/characters/unknown/verbatim-anchors" + suffix,
            json=anchor_body() if method == "POST" else None,
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Voice not found"}
        missing = client.get(BASE + "/missing")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Voice not found"}


@pytest.mark.parametrize("operation", ["register", "get", "list"])
@pytest.mark.parametrize("failure", ["store", "characters"])
def test_unavailable_dependencies_are_sanitized_503(operation: str, failure: str) -> None:
    store = AnchorStore()
    if failure == "store":
        store.error = EvidenceUnavailable("synthetic private connection detail")
    with TestClient(
        application(store, None if failure == "characters" else frozenset({"char-a"}))
    ) as client:
        if failure == "store":
            seed(client)
        response = (
            client.post(BASE, json=anchor_body())
            if operation == "register"
            else client.get(BASE + ("/anchor-1" if operation == "get" else ""))
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Voice unavailable"}
    assert store.values == {}


def test_failed_registration_can_be_retried_without_partial_record() -> None:
    store = AnchorStore(error=EvidenceUnavailable("synthetic failure"))
    with TestClient(application(store)) as client:
        seed(client)
        assert client.post(BASE, json=anchor_body()).status_code == 503
        assert store.values == {}
        store.error = None
        response = client.post(BASE, json=anchor_body())
        assert response.status_code == 201, response.text
        assert client.get(BASE).json() == [response.json()]


def test_reads_follow_current_receipt_and_hide_anchor_after_delivery_is_revoked() -> None:
    store = AnchorStore()
    base = "/characters/char-b/verbatim-anchors"
    with TestClient(application(store)) as client:
        seed(client, perceived_by=["char-a", "char-b"])
        assert (
            client.post(
                "/turns/t-1/receipts", json=receipt_body(status="partial", spoken_until=7)
            ).status_code
            == 201
        )
        created = client.post(base, json=anchor_body())
        assert created.status_code == 201, created.text
        assert created.json()["text"] == TEXT[:7]
        assert (
            client.post(
                "/turns/t-1/receipts", json=receipt_body(status="full", spoken_until=len(TEXT))
            ).status_code
            == 201
        )
        assert client.get(base + "/anchor-1").json()["text"] == TEXT
        assert client.get(base).json()[0]["text"] == TEXT
        assert client.post("/turns/t-1/receipts", json=receipt_body()).status_code == 201
        response = client.get(base + "/anchor-1")
        assert response.status_code == 404
        assert response.json() == {"detail": "Voice not found"}
        assert client.get(base).json() == []
