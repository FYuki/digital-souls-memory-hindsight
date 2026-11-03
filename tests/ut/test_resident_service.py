"""Resident selection uses real visibility rules with isolated persistence."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.character_profile.domain import CharacterProfile
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from digital_souls_memory_hindsight.identity.ports import IdentitySnapshot
from digital_souls_memory_hindsight.recall.ports import ProvisionConflict
from digital_souls_memory_hindsight.voice.domain import Span, VoiceExemplar
from tests.ut.test_delivery_scope import NOW, Clock
from tests.ut.test_evidence_http import MemoryStore
from tests.ut.test_voice_http import TEXT, receipt, seed_turns

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.resident.ports import ResidentProvisionRecord, ResidentRead

pytestmark = pytest.mark.ut
BASE = "/characters/char-a/resident"


@dataclass
class ResidentStore:
    evidence: MemoryStore = field(default_factory=MemoryStore)
    identity_value: IdentitySnapshot = field(
        default_factory=lambda: IdentitySnapshot(None, None, None)
    )
    voices: tuple[VoiceExemplar, ...] = ()
    profile_value: CharacterProfile | None = None
    blocked: frozenset[str] = frozenset()
    records: list[ResidentProvisionRecord] = field(default_factory=list)
    source_sets: list[frozenset[str]] = field(default_factory=list)
    error: Exception | None = None
    changed_once: bool = False
    always_changed: bool = False
    snapshots: int = 0

    def identity(self) -> IdentitySnapshot:
        return self.identity_value

    def exemplars(self) -> tuple[VoiceExemplar, ...]:
        return self.voices

    def profile(self) -> CharacterProfile | None:
        return self.profile_value

    def turns_under_deletion(self, turn_ids: frozenset[str]) -> frozenset[str]:
        return turn_ids & self.blocked

    @contextmanager
    def snapshot(self, character_id: str) -> Iterator[ResidentRead]:
        self.snapshots += 1
        assert self.snapshots <= 10, "Source-change retries must be bounded"
        yield self

    def add(self, record: ResidentProvisionRecord, source_turn_ids: frozenset[str]) -> None:
        if self.error is not None:
            raise self.error
        if self.changed_once or self.always_changed:
            self.changed_once = False
            self.blocked = frozenset({"q"})
            ports = importlib.import_module("digital_souls_memory_hindsight.resident.ports")
            raise ports.ResidentSourcesChanged()
        if any(value.provision_id == record.provision_id for value in self.records):
            raise ProvisionConflict()
        self.records.append(record)
        self.source_sets.append(source_turn_ids)


def application(
    store: ResidentStore, characters: frozenset[str] | None = frozenset({"char-a", "char-b"})
) -> FastAPI:
    module = importlib.import_module("digital_souls_memory_hindsight.resident.service")
    http = importlib.import_module("digital_souls_memory_hindsight.resident_http")
    evidence = EvidenceService(store.evidence, characters, clock=Clock())
    service = module.ResidentService(store, evidence, clock=Clock())
    app = FastAPI()
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(http.resident_router(lambda: service))
    return app


def body(**changes: object) -> dict[str, object]:
    return {"session_id": "s-1", "surface": "Web", **changes}


def seed_selection(store: ResidentStore, client: TestClient) -> None:
    seed_turns(client)
    receipt(client, "a", len(TEXT))
    store.voices = (
        VoiceExemplar(
            "char-a",
            "qa",
            "turn",
            "😀b🍵",
            "😀b",
            Span("a", 1, 4),
            Span("q", 1, 3),
            "synthetic-author",
            NOW,
        ),
        VoiceExemplar(
            "char-a",
            "seed",
            "canonical_seed",
            "abcd",
            None,
            None,
            None,
            "synthetic-author",
            NOW,
        ),
    )
    store.profile_value = CharacterProfile("char-a", 8, 7, (Span("a", 1, 4), Span("q", 0, 5)), NOW)


@pytest.mark.parametrize("query, expected", [(None, ["qa", "seed"]), ("abcd", ["seed", "qa"])])
def test_selection_keeps_qa_paired_ranks_and_records_exact_returned_content(
    query: str | None, expected: list[str]
) -> None:
    store = ResidentStore()
    with TestClient(application(store)) as client:
        seed_selection(store, client)
        response = client.post(
            BASE,
            json=body(
                **(
                    {"query": query, "sensitivity": "public", "classifier_version": "fixture-1"}
                    if query
                    else {}
                )
            ),
        )
    assert response.status_code == 200, response.text
    value = response.json()
    assert [v["exemplar_id"] for v in value["voice_exemplars"]] == expected
    qa = next(v for v in value["voice_exemplars"] if v["exemplar_id"] == "qa")
    assert qa["question"] == "😀b"
    assert qa["answer"] == "😀b🍵"
    assert value["character_profile"] == {
        "revision": 7,
        "entries": [
            {"turn_id": "a", "start": 1, "end": 4, "text": "😀b🍵"},
            {"turn_id": "q", "start": 0, "end": 5, "text": TEXT},
        ],
    }
    assert len(store.records) == 1
    record = store.records[0]
    assert record.provision_id == value["provision_id"]
    assert record.exemplar_ids == tuple(expected)
    assert record.profile_revision == 7
    assert [(s.turn_id, s.start, s.end) for s in record.profile_entries] == [
        ("a", 1, 4),
        ("q", 0, 5),
    ]
    assert store.source_sets == [frozenset({"a", "q"})]
    assert record.created_at == NOW
    assert isinstance(record.processing_time_ms, int) and record.processing_time_ms >= 0
    assert (record.character_id, record.session_id, record.surface) == ("char-a", "s-1", "Web")
    assert record.query == query
    assert record.sensitivity == (None if query is None else "public")
    assert record.classifier_version == (None if query is None else "fixture-1")


@pytest.mark.parametrize("side", ["q", "a"])
@pytest.mark.parametrize("cause", ["receipt", "deletion", "tombstone"])
def test_unavailable_source_removes_whole_qa_and_only_affected_profile_entries(
    side: str, cause: str
) -> None:
    store = ResidentStore()
    with TestClient(application(store)) as client:
        seed_selection(store, client)
        if cause == "receipt":
            # User questions are immediately confirmed; use the mutable scope fixture.
            store.evidence.delivery_scopes = [
                replace(s, spoken_until=2) if s.turn_id == side else s
                for s in store.evidence.delivery_scopes
            ]
        elif cause == "deletion":
            store.blocked = frozenset({side})
        else:
            del store.evidence.turns[side]
            store.evidence.tombstones.add(side)
        response = client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["voice_exemplars"] == [{"exemplar_id": "seed", "question": None, "answer": "abcd"}]
    kept = "a" if side == "q" else "q"
    assert [e["turn_id"] for e in value["character_profile"]["entries"]] == [kept]
    assert value["character_profile"]["revision"] == 7
    assert store.records[0].exemplar_ids == ("seed",)
    assert [s.turn_id for s in store.records[0].profile_entries] == [kept]
    assert store.source_sets == [frozenset({kept})]


def test_source_change_retries_selection_and_saves_only_rechecked_response() -> None:
    store = ResidentStore(changed_once=True)
    with TestClient(application(store)) as client:
        seed_selection(store, client)
        response = client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    assert [v["exemplar_id"] for v in response.json()["voice_exemplars"]] == ["seed"]
    assert [e["turn_id"] for e in response.json()["character_profile"]["entries"]] == ["a"]
    assert len(store.records) == 1
    assert store.records[0].exemplar_ids == ("seed",)
    assert [s.turn_id for s in store.records[0].profile_entries] == ["a"]
    assert store.snapshots == 2


def test_repeated_source_changes_stop_without_a_success_or_saved_record() -> None:
    store = ResidentStore(always_changed=True)
    with TestClient(application(store)) as client:
        seed_selection(store, client)
        response = client.post(BASE, json=body())
    assert response.status_code == 503
    assert store.records == []


def test_processing_time_is_measured_in_milliseconds_with_the_injected_timer() -> None:
    store = ResidentStore()
    with TestClient(application(store)) as client:
        seed_turns(client)
    module = importlib.import_module("digital_souls_memory_hindsight.resident.service")
    ticks = iter((10.0, 10.125))
    service = module.ResidentService(
        store,
        EvidenceService(store.evidence, frozenset({"char-a"})),
        clock=Clock(),
        timer=lambda: next(ticks),
    )
    service.provide("char-a", "s-1", "Web", None, None, None)
    assert store.records[0].processing_time_ms == 125


@pytest.mark.parametrize(
    "error, status", [(EvidenceUnavailable(), 503), (ProvisionConflict(), 409)]
)
def test_failed_record_commit_never_returns_success_and_next_request_can_recover(
    error: Exception, status: int
) -> None:
    store = ResidentStore(error=error)
    with TestClient(application(store)) as client:
        seed_turns(client)
        assert client.post(BASE, json=body()).status_code == status
        assert store.records == []
        store.error = None
        response = client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    assert [v.provision_id for v in store.records] == [response.json()["provision_id"]]
