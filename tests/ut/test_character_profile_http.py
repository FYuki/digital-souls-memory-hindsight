"""Profile HTTP contracts with real evidence and an isolated persistence fake."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from digital_souls_memory_hindsight.voice.domain import Span
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import MemoryStore, turn_body
from tests.ut.test_voice_http import DeletionState, receipt, seed_turns, span

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.character_profile.domain import CharacterProfile

pytestmark = pytest.mark.ut
BASE = "/characters/char-a/profile"


def profile_body(**changes: object) -> dict[str, object]:
    return {"char_limit": 5, "entries": [span("q", 0, 5)], **changes}


@dataclass
class ProfileStore:
    values: dict[str, CharacterProfile] = field(default_factory=dict)
    error: Exception | None = None

    def save(
        self, character_id: str, char_limit: int, entries: tuple[Span, ...], updated_at: datetime
    ) -> CharacterProfile:
        if self.error is not None:
            raise self.error
        domain = importlib.import_module("digital_souls_memory_hindsight.character_profile.domain")
        previous = self.values.get(character_id)
        value: CharacterProfile = domain.CharacterProfile(
            character_id,
            char_limit,
            1 if previous is None else previous.revision + 1,
            entries,
            updated_at,
        )
        self.values[character_id] = value
        return value

    def get(self, character_id: str) -> CharacterProfile | None:
        if self.error is not None:
            raise self.error
        return self.values.get(character_id)


def application(
    store: ProfileStore,
    deletion: DeletionState,
    ledger: MemoryStore,
    characters: frozenset[str] | None = frozenset({"char-a", "char-b"}),
) -> FastAPI:
    module = importlib.import_module("digital_souls_memory_hindsight.character_profile.service")
    http = importlib.import_module("digital_souls_memory_hindsight.character_profile_http")
    clock = Clock()
    evidence = EvidenceService(ledger, characters, clock=clock)
    service = module.CharacterProfileService(store, evidence, deletion, clock=clock)
    app = FastAPI()
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(http.character_profile_router(lambda: service))
    return app


@pytest.mark.parametrize("start,end", [(0, 5), (1, 4)])
def test_roundtrip_uses_codepoints_and_increments_revision(start: int, end: int) -> None:
    store = ProfileStore()
    body = profile_body(char_limit=end - start, entries=[span("q", start, end)])
    with TestClient(application(store, DeletionState(), MemoryStore())) as client:
        seed_turns(client)
        for revision in (1, 2):
            saved = client.put(BASE, json=body)
            assert saved.status_code == 200, saved.text
            value = saved.json()
            assert value["character_id"] == "char-a"
            assert value["char_limit"] == end - start
            assert value["entries"] == body["entries"]
            assert value["revision"] == revision
            assert datetime.fromisoformat(value["updated_at"]) == Clock().now
            assert client.get(BASE).json() == value


@pytest.mark.parametrize(
    "changes",
    [
        {"char_limit": -1},
        {"char_limit": 1.5},
        {"char_limit": True},
        {"char_limit": "5"},
        {"char_limit": 2**63},
        {"memory_ids": ["synthetic-memory"]},
        {"entries": [{**span("q", 0, 1), "memory_id": "synthetic-memory"}]},
        {"entries": [span("q", -1, 2)]},
        {"entries": [span("q", 2, 2)]},
        {"entries": [span("q", 4, 2)]},
        {"entries": [span("q", 0, 6)]},
        {"entries": [span("q", True, 2)]},
        {"entries": [{"turn_id": "q", "start": 0, "end": 1.5}]},
        {"entries": [span("missing", 0, 1)]},
        {"entries": [span("q", 0, 3), span("q", 2, 4)]},
        {"entries": [span("q", 0, 2), span("q", 0, 2)]},
        {"char_limit": 3, "entries": [span("q", 0, 2), span("other", 0, 2)]},
        {"entries": None},
    ],
)
def test_invalid_put_returns_422_and_preserves_entire_previous_setting(
    changes: dict[str, object],
) -> None:
    store = ProfileStore()
    with TestClient(application(store, DeletionState(), MemoryStore())) as client:
        seed_turns(client)
        first = client.put(BASE, json=profile_body())
        assert first.status_code == 200, first.text
        assert client.put(BASE, json=profile_body(**changes)).status_code == 422
        assert client.get(BASE).json() == first.json()
        assert store.values["char-a"].revision == 1


def test_adjacent_intervals_and_same_offsets_on_different_turns_are_accepted() -> None:
    entries = [span("q", 2, 4), span("q", 0, 2), span("other", 0, 2)]
    with TestClient(application(ProfileStore(), DeletionState(), MemoryStore())) as client:
        seed_turns(client)
        response = client.put(BASE, json=profile_body(char_limit=6, entries=entries))
        assert response.status_code == 200, response.text
        assert response.json()["entries"] == entries


def test_zero_limit_with_empty_entries_replaces_previous_setting() -> None:
    with TestClient(application(ProfileStore(), DeletionState(), MemoryStore())) as client:
        seed_turns(client)
        assert client.put(BASE, json=profile_body()).status_code == 200
        response = client.put(BASE, json=profile_body(char_limit=0, entries=[]))
        assert response.status_code == 200, response.text
        assert response.json()["entries"] == []
        assert response.json()["char_limit"] == 0
        assert response.json()["revision"] == 2
        assert client.get(BASE).json() == response.json()


@pytest.mark.parametrize(
    "condition", ["unperceived", "pending", "partial", "thought", "deleted", "tombstone"]
)
def test_every_entry_is_checked_before_persistence(condition: str) -> None:
    store, deletion, ledger = ProfileStore(), DeletionState(), MemoryStore()
    with TestClient(application(store, deletion, ledger)) as client:
        seed_turns(client)
        first = client.put(BASE, json=profile_body())
        assert first.status_code == 200, first.text
        if condition == "unperceived":
            assert (
                client.post(
                    "/turns", json=turn_body(turn_id="hidden", actor="user-a", perceived_by=[])
                ).status_code
                == 201
            )
            identifier = "hidden"
        else:
            identifier = "a"
        if condition == "partial":
            receipt(client, "a", 2)
        if condition == "thought":
            assert (
                client.post("/turns", json=turn_body(turn_id="thought", kind="thought")).status_code
                == 201
            )
            identifier = "thought"
        if condition == "deleted":
            receipt(client, "a", 5)
            deletion.blocked = frozenset({"a"})
        if condition == "tombstone":
            del ledger.turns["a"]
            ledger.tombstones.add("a")
        response = client.put(
            BASE, json=profile_body(char_limit=8, entries=[span("q", 0, 5), span(identifier, 0, 3)])
        )
        assert response.status_code == 422, response.text
        assert client.get(BASE).json() == first.json()
        assert store.values["char-a"].revision == 1


@pytest.mark.parametrize("condition", ["receipt", "deletion", "perception", "tombstone"])
def test_get_rechecks_visibility_without_changing_saved_entries_or_revision(condition: str) -> None:
    store, deletion, ledger = ProfileStore(), DeletionState(), MemoryStore()
    body = profile_body(char_limit=8, entries=[span("q", 0, 5), span("a", 1, 4)])
    with TestClient(application(store, deletion, ledger)) as client:
        seed_turns(client)
        receipt(client, "a", 5)
        saved = client.put(BASE, json=body)
        assert saved.status_code == 200, saved.text
        assert client.get(BASE).json() == saved.json()
        persisted = store.values["char-a"]
        if condition == "receipt":
            receipt(client, "a", 3)
        elif condition == "deletion":
            deletion.blocked = frozenset({"a"})
        elif condition == "perception":
            ledger.turns["a"] = replace(ledger.turns["a"], perceived_by=())
        else:
            del ledger.turns["a"]
            ledger.tombstones.add("a")
        response = client.get(BASE)
        assert response.status_code == 200, response.text
        assert response.json() == {**saved.json(), "entries": [span("q", 0, 5)]}
        assert store.values["char-a"] == persisted
        if condition == "receipt":
            receipt(client, "a", 5)
            assert client.get(BASE).json() == saved.json()


@pytest.mark.parametrize("operation", ["put", "get"])
@pytest.mark.parametrize("failure", ["store", "characters"])
def test_unavailable_dependencies_return_503(operation: str, failure: str) -> None:
    store = ProfileStore(
        error=EvidenceUnavailable("synthetic failure") if failure == "store" else None
    )
    with TestClient(
        application(
            store,
            DeletionState(),
            MemoryStore(),
            None if failure == "characters" else frozenset({"char-a"}),
        )
    ) as client:
        response = (
            client.put(BASE, json=profile_body(char_limit=0, entries=[]))
            if operation == "put"
            else client.get(BASE)
        )
        assert response.status_code == 503
        assert "synthetic failure" not in response.text


def test_unknown_character_and_unconfigured_profile_are_404() -> None:
    with TestClient(application(ProfileStore(), DeletionState(), MemoryStore())) as client:
        assert client.get(BASE).status_code == 404
        assert client.get(BASE.replace("char-a", "unknown")).status_code == 404
        assert (
            client.put(
                BASE.replace("char-a", "unknown"), json=profile_body(char_limit=0, entries=[])
            ).status_code
            == 404
        )


def test_create_app_exposes_profile_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    from digital_souls_memory_hindsight.app import create_app

    assert {"put", "get"} <= set(
        create_app().openapi()["paths"]["/characters/{character_id}/profile"]
    )


def test_production_lifespan_wires_profile_service_to_put_and_get(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    store = ProfileStore()
    monkeypatch.setattr(module, "PostgresCharacterProfileStore", MagicMock(return_value=store))
    monkeypatch.setattr(module, "PostgresDeletionStore", MagicMock(return_value=DeletionState()))
    worker = MagicMock()
    worker.run = AsyncMock()
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=worker))
    with TestClient(module.create_app()) as client:
        body = profile_body(char_limit=0, entries=[])
        saved = client.put(BASE, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["entries"] == []
        assert saved.json()["revision"] == 1
        assert client.get(BASE).json() == saved.json()
