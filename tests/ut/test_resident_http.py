"""Resident HTTP input and public identity representation."""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.identity.ports import (
    CoreIdentity,
    IdentitySnapshot,
    PersonalityProfile,
    Temperament,
)
from tests.ut.test_delivery_scope import NOW
from tests.ut.test_evidence_http import MemoryStore, session_body
from tests.ut.test_resident_service import BASE, ResidentStore, application, body

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "changes, omitted",
    [
        ({}, "session_id"),
        ({}, "surface"),
        ({"session_id": " \t"}, None),
        ({"surface": " \n"}, None),
        ({"session_id": 42}, None),
        ({"surface": True}, None),
        ({"query": "茶"}, None),
        ({"query": "茶", "sensitivity": "public"}, None),
        ({"query": "茶", "classifier_version": "fixture-1"}, None),
        ({"query": "茶", "sensitivity": "", "classifier_version": "fixture-1"}, None),
        ({"query": "茶", "sensitivity": "public", "classifier_version": " \t"}, None),
        ({"query": " "}, None),
        ({"query": 42}, None),
        ({"turn_id": "t-1"}, None),
        ({"extra": "unexpected"}, None),
    ],
)
def test_invalid_request_returns_422_without_creating_a_record(
    changes: dict[str, object], omitted: str | None
) -> None:
    store = ResidentStore()
    request = body(**changes)
    if omitted is not None:
        del request[omitted]
    with TestClient(application(store)) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        assert client.post(BASE, json=request).status_code == 422
    assert store.records == []


@pytest.mark.parametrize("registered", ["none", "partial", "all"])
def test_identity_response_contains_only_public_fields_and_records_returned_versions(
    registered: str,
) -> None:
    core = CoreIdentity("char-a", 2, ("約束", "対話"), ("攻撃",), NOW)
    temperament = Temperament("char-a", 3, "穏やかな架空の気質", NOW)
    personality = PersonalityProfile("char-a", 1, {"Openness": 0.8}, "架空の記述", "r1", 3, NOW)
    store = ResidentStore(
        identity_value=IdentitySnapshot(
            core if registered != "none" else None,
            temperament if registered == "all" else None,
            personality if registered == "all" else None,
        )
    )
    with TestClient(application(store)) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        response = client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    value = response.json()
    record = store.records[0]
    assert value == {
        "provision_id": record.provision_id,
        "core_identity": {"version": 2, "values": ["約束", "対話"], "prohibitions": ["攻撃"]}
        if registered != "none"
        else None,
        "temperament": {"version": 3, "description": temperament.description}
        if registered == "all"
        else None,
        "personality_profile": {"version": 1, "description": personality.description}
        if registered == "all"
        else None,
        "voice_exemplars": [],
        "character_profile": None,
        "motivations": [],
    }
    assert record.core_identity_version == (None if registered == "none" else 2)
    assert record.temperament_version == (3 if registered == "all" else None)
    assert record.personality_version == (1 if registered == "all" else None)
    assert record.exemplar_ids == ()
    assert record.profile_revision is None
    assert record.profile_entries == ()


def test_missing_session_returns_404_without_creating_a_record() -> None:
    store = ResidentStore()
    with TestClient(application(store)) as client:
        assert client.post(BASE, json=body(session_id="missing")).status_code == 404
    assert store.records == []


@pytest.mark.parametrize("characters, status", [(frozenset({"char-b"}), 404), (None, 503)])
def test_unknown_or_unconfigured_character_cannot_create_a_record(
    characters: frozenset[str] | None, status: int
) -> None:
    store = ResidentStore()
    with TestClient(application(store, characters)) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        assert client.post(BASE, json=body()).status_code == status
    assert store.records == []


def test_normalized_query_and_classification_reach_the_record() -> None:
    store = ResidentStore()
    with TestClient(application(store)) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        response = client.post(
            BASE,
            json=body(
                session_id=" s-1 ",
                surface=" Windows ",
                query=" 茶 ",
                sensitivity=" public ",
                classifier_version=" fixture-1 ",
            ),
        )
    assert response.status_code == 200, response.text
    record = store.records[0]
    assert (record.session_id, record.surface, record.query) == ("s-1", "Windows", "茶")
    assert (record.sensitivity, record.classifier_version) == ("public", "fixture-1")


def test_production_lifespan_exposes_resident_without_hindsight_or_model_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    store = ResidentStore()
    monkeypatch.setattr(module, "PostgresResidentStore", MagicMock(return_value=store))
    monkeypatch.setattr(module, "PostgresEvidenceStore", MagicMock(return_value=MemoryStore()))
    worker = MagicMock()
    worker.run = AsyncMock()
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=worker))
    with TestClient(module.create_app()) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        response = client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    assert response.json()["provision_id"] == store.records[0].provision_id
