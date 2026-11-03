"""Independent state endpoint, including production route registration."""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceNotFound, EvidenceUnavailable
from tests.ut.test_evidence_http import MemoryStore, session_body

pytestmark = pytest.mark.ut
BASE = "/characters/char-a/sessions/s-1/state"


@pytest.mark.parametrize("generated", [False, True])
def test_state_response_has_exact_public_fields(generated: bool) -> None:
    http = importlib.import_module("digital_souls_memory_hindsight.character_state_http")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    service = MagicMock()
    service.get.return_value = (
        domain.CharacterState("穏やか", "温かい", "お茶") if generated else None
    )
    app = FastAPI()
    app.include_router(http.character_state_router(lambda: service))
    with TestClient(app) as client:
        response = client.get(BASE, params={"surface": "Web"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "state": {
            "mood": "穏やか",
            "relationship_temperature": "温かい",
            "current_topic": "お茶",
        }
        if generated
        else None
    }
    service.get.assert_called_once_with("char-a", "s-1", "Web")


@pytest.mark.parametrize("surface", [None, "", "  "])
def test_surface_is_required_and_nonblank(surface: str | None) -> None:
    http = importlib.import_module("digital_souls_memory_hindsight.character_state_http")
    service = MagicMock()
    app = FastAPI()
    app.include_router(http.character_state_router(lambda: service))
    with TestClient(app) as client:
        response = client.get(BASE, params={} if surface is None else {"surface": surface})
    assert response.status_code == 422
    service.get.assert_not_called()


@pytest.mark.parametrize("error,status", [(EvidenceNotFound, 404), (EvidenceUnavailable, 503)])
def test_errors_are_converted_at_http_boundary(error: type[Exception], status: int) -> None:
    http = importlib.import_module("digital_souls_memory_hindsight.character_state_http")
    service = MagicMock()
    service.get.side_effect = error("synthetic internal detail")
    app = FastAPI()
    app.include_router(http.character_state_router(lambda: service))
    with TestClient(app) as client:
        response = client.get(BASE, params={"surface": "Web"})
    assert response.status_code == status
    assert "synthetic internal detail" not in response.text


def test_create_app_exposes_state_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    from digital_souls_memory_hindsight.app import create_app

    assert (
        "get"
        in create_app().openapi()["paths"]["/characters/{character_id}/sessions/{session_id}/state"]
    )


def test_production_app_without_gateway_returns_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    storage = MagicMock()
    storage.get.return_value = None
    deletion = MagicMock()
    deletion.blocked_sessions.return_value = frozenset()
    monkeypatch.setattr(module, "PostgresEvidenceStore", MagicMock(return_value=MemoryStore()))
    monkeypatch.setattr(module, "PostgresCharacterStateStore", MagicMock(return_value=storage))
    monkeypatch.setattr(module, "PostgresDeletionStore", MagicMock(return_value=deletion))
    worker = MagicMock()
    worker.run = AsyncMock()
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=worker))
    with TestClient(module.create_app()) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        response = client.get(BASE, params={"surface": "Web"})
        assert response.status_code == 200, response.text
        assert response.json() == {"state": None}
