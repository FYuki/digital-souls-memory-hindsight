"""Approved voice exemplars through HTTP, with real evidence validation."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from tests.consistency_doubles import Gateway, Reviewer
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import MemoryStore, receipt_body, session_body, turn_body
from tests.ut.test_identity_service import IdentityStore

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.voice.domain import VoiceExemplar, VoiceReview

pytestmark = pytest.mark.ut
BASE = "/characters/char-a/voice-exemplars"
TEXT = "a😀b🍵c"
SEED = "  架空のＡＢＣ🍵\r\n\t「記号」<&>  "


def voice_body(**changes: object) -> dict[str, object]:
    return {
        "exemplar_id": "voice-1",
        "source": "canonical_seed",
        "answer": SEED,
        "approved_by": " synthetic-author \t",
        **changes,
    }


def span(turn_id: str, start: int, end: int) -> dict[str, object]:
    return {"turn_id": turn_id, "start": start, "end": end}


def turn_voice(**changes: object) -> dict[str, object]:
    return voice_body(
        **{
            "source": "turn",
            "answer": "😀b🍵",
            "answer_span": span("a", 1, 4),
            **changes,
        }
    )


@dataclass
class VoiceStore:
    values: dict[tuple[str, str], VoiceExemplar] = field(default_factory=dict)
    error: Exception | None = None
    reviews: dict[str, VoiceReview] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    def reviewed(self, character_id: str) -> bool:
        if self.error is not None:
            raise self.error
        return character_id in self.reviews

    def add(self, value: VoiceExemplar, review: VoiceReview | None) -> bool:
        if self.error is not None:
            raise self.error
        with self.lock:
            key = (value.character_id, value.exemplar_id)
            if key in self.values:
                ports = importlib.import_module("digital_souls_memory_hindsight.voice.ports")
                raise ports.VoiceConflict()
            self.values[key] = value
            if review is not None and value.character_id not in self.reviews:
                self.reviews[value.character_id] = review
                return True
            return False

    def get(self, character_id: str, exemplar_id: str) -> VoiceExemplar | None:
        if self.error is not None:
            raise self.error
        return self.values.get((character_id, exemplar_id))

    def list(self, character_id: str) -> tuple[VoiceExemplar, ...]:
        if self.error is not None:
            raise self.error
        return tuple(v for (character, _), v in self.values.items() if character == character_id)


@dataclass
class DeletionState:
    blocked: frozenset[str] = frozenset()

    def turns_under_deletion(self, turn_ids: frozenset[str]) -> frozenset[str]:
        return self.blocked & turn_ids


def application(
    store: VoiceStore,
    deletion: DeletionState,
    ledger: MemoryStore,
    clock: Clock,
    characters: frozenset[str] | None = frozenset({"char-a", "char-b"}),
) -> FastAPI:
    module = importlib.import_module("digital_souls_memory_hindsight.voice.service")
    http = importlib.import_module("digital_souls_memory_hindsight.voice_http")
    evidence = EvidenceService(ledger, characters, clock=clock)
    voice = module.VoiceService(
        store, evidence, deletion, IdentityStore(), Gateway(), Reviewer(), clock=clock
    )
    app = FastAPI()
    app.state.evidence = evidence
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(http.voice_router(lambda: voice))
    return app


def seed_turns(client: TestClient, *, answer_changes: dict[str, object] | None = None) -> None:
    assert client.post("/sessions", json=session_body()).status_code == 201
    for identifier in ("q", "other"):
        response = client.post(
            "/turns",
            json=turn_body(turn_id=identifier, actor="user-a", text=TEXT),
        )
        assert response.status_code == 201, response.text
    response = client.post(
        "/turns",
        json=turn_body(
            **{
                "turn_id": "a",
                "text": TEXT,
                "reply_to_turn_id": "q",
                **(answer_changes or {}),
            }
        ),
    )
    assert response.status_code == 201, response.text


def receipt(client: TestClient, identifier: str, end: int) -> None:
    status = "none" if end == 0 else "full" if end == len(TEXT) else "partial"
    response = client.post(
        f"/turns/{identifier}/receipts", json=receipt_body(status=status, spoken_until=end)
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("question", [None, SEED, " \t\n"])
def test_seed_roundtrip_preserves_verbatim_answer_question_and_approval(
    question: str | None,
) -> None:
    store = VoiceStore()
    body = voice_body(**({} if question is None else {"question": question}))
    with TestClient(application(store, DeletionState(), MemoryStore(), Clock())) as client:
        response = client.post(BASE, json=body)
        assert response.status_code == 201, response.text
        saved = response.json()
        saved.pop("consistency_review")
        for key, value in body.items():
            assert saved[key] == value
        assert saved.get("question") == question
        assert "trait_version" not in saved
        assert client.get(BASE + "/voice-1").json() == saved
        assert client.get(BASE).json() == [saved]


@pytest.mark.parametrize(
    "changes, omitted",
    [
        ({}, "answer"),
        ({}, "approved_by"),
        ({}, "source"),
        ({"answer": ""}, None),
        ({"question": ""}, None),
        ({"approved_by": ""}, None),
        ({"approved_by": " \t\n"}, None),
        ({"source": "unknown"}, None),
        ({"trait_version": 1}, None),
        ({"answer_span": span("a", 0, 1)}, None),
        ({"question_span": span("q", 0, 1)}, None),
    ],
)
def test_invalid_fields_are_422_without_persistence(
    changes: dict[str, object], omitted: str | None
) -> None:
    store = VoiceStore()
    body = voice_body(**changes)
    if omitted is not None:
        del body[omitted]
    with TestClient(application(store, DeletionState(), MemoryStore(), Clock())) as client:
        assert client.post(BASE, json=body).status_code == 422
    assert store.values == {}


@pytest.mark.parametrize("qa", [False, True])
def test_turn_codepoint_spans_roundtrip_without_normalization(qa: bool) -> None:
    body = turn_voice(**({"question": "😀b", "question_span": span("q", 1, 3)} if qa else {}))
    with TestClient(application(VoiceStore(), DeletionState(), MemoryStore(), Clock())) as client:
        seed_turns(client)
        receipt(client, "a", 4)
        response = client.post(BASE, json=body)
        assert response.status_code == 201, response.text
        saved = response.json()
        saved.pop("consistency_review")
        for key, value in body.items():
            assert saved[key] == value
        assert client.get(BASE + "/voice-1").json() == saved
        assert client.get(BASE).json() == [saved]


@pytest.mark.parametrize(
    "changes",
    [
        {"answer_span": None},
        {"answer_span": span("a", -1, 4)},
        {"answer_span": span("a", 1, 1)},
        {"answer_span": span("a", 4, 1)},
        {"answer_span": span("a", 1, 3)},
        {"answer_span": span("a", 1, 6), "answer": "😀b🍵c"},
        {"answer": "a😀b🍵c"},
        {"answer_span": span("missing", 1, 4)},
        {"question": "😀b"},
        {"question_span": span("q", 1, 3)},
        {"question": "😀b", "question_span": span("other", 1, 3)},
        {"question": "😀b🍵", "question_span": span("q", 1, 3)},
    ],
)
def test_invalid_turn_spans_or_reply_target_are_422_without_persistence(
    changes: dict[str, object],
) -> None:
    store = VoiceStore()
    with TestClient(application(store, DeletionState(), MemoryStore(), Clock())) as client:
        seed_turns(client)
        receipt(client, "a", len(TEXT))
        assert client.post(BASE, json=turn_voice(**changes)).status_code == 422
    assert store.values == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"actor": "char-b", "perceived_by": ["char-a", "char-b"]},
        {"kind": "observation"},
        {"kind": "thought"},
        {"reply_to_turn_id": None},
    ],
)
def test_answer_must_be_own_utterance_and_question_requires_reply(
    changes: dict[str, object],
) -> None:
    store = VoiceStore()
    with TestClient(application(store, DeletionState(), MemoryStore(), Clock())) as client:
        seed_turns(client, answer_changes=changes)
        if changes.get("kind") != "thought":
            receipt(client, "a", len(TEXT))
        body = turn_voice(question="😀b", question_span=span("q", 1, 3))
        assert client.post(BASE, json=body).status_code == 422
    assert store.values == {}


@pytest.mark.parametrize("limit", [None, 0, 3])
def test_own_answer_requires_confirmed_delivery_covering_entire_span(limit: int | None) -> None:
    store = VoiceStore()
    with TestClient(application(store, DeletionState(), MemoryStore(), Clock())) as client:
        seed_turns(client)
        if limit is not None:
            receipt(client, "a", limit)
        assert client.post(BASE, json=turn_voice()).status_code == 422
    assert store.values == {}


def test_assumed_confirmation_makes_same_pending_answer_registerable() -> None:
    clock = Clock()
    app = application(VoiceStore(), DeletionState(), MemoryStore(), clock)
    with TestClient(app) as client:
        seed_turns(client)
        assert client.post(BASE, json=turn_voice()).status_code == 422
        clock.now += timedelta(seconds=600)
        app.state.evidence.confirm_expired_deliveries()
        assert client.post(BASE, json=turn_voice()).status_code == 201


def test_unperceived_reply_question_is_rejected_without_persistence() -> None:
    store = VoiceStore()
    with TestClient(application(store, DeletionState(), MemoryStore(), Clock())) as client:
        seed_turns(client)
        receipt(client, "a", len(TEXT))
        assert (
            client.post(
                "/turns",
                json=turn_body(turn_id="hidden", actor="user-a", text=TEXT, perceived_by=[]),
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/turns", json=turn_body(turn_id="reply", text=TEXT, reply_to_turn_id="hidden")
            ).status_code
            == 201
        )
        receipt(client, "reply", len(TEXT))
        body = turn_voice(
            answer_span=span("reply", 1, 4), question="😀b", question_span=span("hidden", 1, 3)
        )
        assert client.post(BASE, json=body).status_code == 422
    assert store.values == {}


@pytest.mark.parametrize("limit", [None, 0, 2])
def test_question_requires_delivery_of_entire_span(limit: int | None) -> None:
    store = VoiceStore()
    with TestClient(application(store, DeletionState(), MemoryStore(), Clock())) as client:
        seed_turns(client)
        assert client.post("/turns", json=turn_body(turn_id="cq", text=TEXT)).status_code == 201
        if limit is not None:
            receipt(client, "cq", limit)
        assert (
            client.post(
                "/turns", json=turn_body(turn_id="ca", text=TEXT, reply_to_turn_id="cq")
            ).status_code
            == 201
        )
        receipt(client, "ca", len(TEXT))
        body = turn_voice(
            answer_span=span("ca", 1, 4), question="😀b", question_span=span("cq", 1, 3)
        )
        assert client.post(BASE, json=body).status_code == 422
    assert store.values == {}


@pytest.mark.parametrize("side", ["q", "a"])
def test_reads_recheck_current_delivery_and_exclude_entire_qa(side: str) -> None:
    with TestClient(application(VoiceStore(), DeletionState(), MemoryStore(), Clock())) as client:
        seed_turns(client)
        receipt(client, "a", len(TEXT))
        # A character question has a revisable receipt, unlike an immediate user turn.
        assert (
            client.post(
                "/turns", json=turn_body(turn_id="cq", text=TEXT, perceived_by=["char-a", "char-b"])
            ).status_code
            == 201
        )
        receipt(client, "cq", len(TEXT))
        assert (
            client.post(
                "/turns", json=turn_body(turn_id="ca", text=TEXT, reply_to_turn_id="cq")
            ).status_code
            == 201
        )
        receipt(client, "ca", len(TEXT))
        body = turn_voice(
            answer_span=span("ca", 1, 4), question="😀b", question_span=span("cq", 1, 3)
        )
        created = client.post(BASE, json=body)
        assert created.status_code == 201, created.text
        assert client.post(BASE, json=voice_body(exemplar_id="kept")).status_code == 201
        assert client.get(BASE + "/voice-1").json() == {
            k: v for k, v in created.json().items() if k != "consistency_review"
        }
        receipt(client, "cq" if side == "q" else "ca", 2)
        assert client.get(BASE + "/voice-1").status_code == 404
        assert [item["exemplar_id"] for item in client.get(BASE).json()] == ["kept"]


@pytest.mark.parametrize("side", ["q", "a"])
def test_deletion_acceptance_hides_existing_qa_and_rejects_new_registration(side: str) -> None:
    deletion = DeletionState()
    store = VoiceStore()
    with TestClient(application(store, deletion, MemoryStore(), Clock())) as client:
        seed_turns(client)
        receipt(client, "a", len(TEXT))
        body = turn_voice(question="😀b", question_span=span("q", 1, 3))
        created = client.post(BASE, json=body)
        assert created.status_code == 201, created.text
        assert client.get(BASE + "/voice-1").status_code == 200
        deletion.blocked = frozenset({side})
        assert client.get(BASE + "/voice-1").status_code == 404
        assert client.get(BASE).json() == []
        assert client.post(BASE, json={**body, "exemplar_id": "new"}).status_code == 422
        assert len(store.values) == 1


def test_tombstoned_source_is_422_without_persistence() -> None:
    ledger = MemoryStore()
    store = VoiceStore()
    with TestClient(application(store, DeletionState(), ledger, Clock())) as client:
        seed_turns(client)
        receipt(client, "a", len(TEXT))
        del ledger.turns["a"]
        ledger.tombstones.add("a")
        assert client.post(BASE, json=turn_voice()).status_code == 422
    assert store.values == {}


@pytest.mark.parametrize(
    "query, expected", [(None, ["a", "b", "c", "d"]), ("abcd", ["b", "c", "d", "a"])]
)
def test_list_ranks_maximum_of_question_and_answer_with_id_tiebreak(
    query: str | None, expected: list[str]
) -> None:
    with TestClient(application(VoiceStore(), DeletionState(), MemoryStore(), Clock())) as client:
        for identifier, answer, question in [
            ("d", "abcd", "unrelated"),
            ("a", "ab", "ab"),
            ("c", "abcd", None),
            ("b", "unrelated", "abcd"),
        ]:
            body = voice_body(exemplar_id=identifier, answer=answer)
            if question is not None:
                body["question"] = question
            assert client.post(BASE, json=body).status_code == 201
        response = client.get(BASE, params={} if query is None else {"query": query})
        assert response.status_code == 200
        assert [item["exemplar_id"] for item in response.json()] == expected


def test_duplicate_is_409_and_keeps_first_value_scoped_to_character() -> None:
    with TestClient(application(VoiceStore(), DeletionState(), MemoryStore(), Clock())) as client:
        first = client.post(BASE, json=voice_body())
        assert first.status_code == 201
        assert client.post(BASE, json=voice_body(answer="置換禁止")).status_code == 409
        saved = {k: v for k, v in first.json().items() if k != "consistency_review"}
        assert client.get(BASE + "/voice-1").json() == saved
        other = client.post(BASE.replace("char-a", "char-b"), json=voice_body())
        assert other.status_code == 201
        assert client.get(BASE).json() == [saved]


@pytest.mark.parametrize("operation", ["register", "get", "list"])
@pytest.mark.parametrize("failure", ["store", "characters"])
def test_unavailable_dependencies_return_503(operation: str, failure: str) -> None:
    store = VoiceStore(
        error=EvidenceUnavailable("synthetic failure") if failure == "store" else None
    )
    app = application(
        store,
        DeletionState(),
        MemoryStore(),
        Clock(),
        None if failure == "characters" else frozenset({"char-a"}),
    )
    with TestClient(app) as client:
        response = (
            client.post(BASE, json=voice_body())
            if operation == "register"
            else client.get(BASE + ("/voice-1" if operation == "get" else ""))
        )
        assert response.status_code == 503
        assert "synthetic failure" not in response.text


def test_unknown_character_and_missing_exemplar_are_404() -> None:
    with TestClient(application(VoiceStore(), DeletionState(), MemoryStore(), Clock())) as client:
        assert client.post(BASE.replace("char-a", "unknown"), json=voice_body()).status_code == 404
        assert client.get(BASE + "/missing").status_code == 404


def test_create_app_exposes_voice_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    from digital_souls_memory_hindsight.app import create_app

    schema = create_app().openapi()
    assert {"post", "get"} <= set(schema["paths"]["/characters/{character_id}/voice-exemplars"])
    assert "get" in schema["paths"]["/characters/{character_id}/voice-exemplars/{exemplar_id}"]
