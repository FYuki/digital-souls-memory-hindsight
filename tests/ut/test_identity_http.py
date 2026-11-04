"""Identity input rejection at the production HTTP entry point, without I/O."""

import importlib
import json
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.ut


def seed_body() -> dict[str, object]:
    return {
        "aspects": {
            "Volatility": 0,
            "Withdrawal": 1,
            "Compassion": 0.5,
            "Politeness": 0.3,
            "Industriousness": 0.3,
            "Orderliness": 0.3,
            "Enthusiasm": 0.3,
            "Assertiveness": 0.3,
            "Intellect": 0.3,
            "Openness": 0.3,
        },
        "renderer_version": "fixture-renderer-1",
    }


@pytest.fixture
def identity_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_ID", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_TOKEN", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    return cast(FastAPI, module.create_app())


@pytest.mark.parametrize("value", ["0.5", True, False, None, -0.0001, 1.0000001])
def test_seed_rejects_invalid_aspect_values_without_echoing_input(
    identity_app: FastAPI, value: object
) -> None:
    body = seed_body()
    aspects = body["aspects"]
    assert isinstance(aspects, dict)
    aspects["Openness"] = value
    response = TestClient(identity_app).post(
        "/characters/char-a/personality-profiles/author-seed", json=body
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid identity value"}


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_seed_rejects_nonfinite_json_without_echoing_aspects(
    identity_app: FastAPI, token: str
) -> None:
    raw = json.dumps(seed_body()).replace('"Openness": 0.3', '"Openness": ' + token)
    response = TestClient(identity_app).post(
        "/characters/char-a/personality-profiles/author-seed",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid identity value"}


@pytest.mark.parametrize(
    "key",
    [
        "Volatility",
        "Withdrawal",
        "Compassion",
        "Politeness",
        "Industriousness",
        "Orderliness",
        "Enthusiasm",
        "Assertiveness",
        "Intellect",
        "Openness",
    ],
)
def test_seed_requires_every_aspect(identity_app: FastAPI, key: str) -> None:
    body = seed_body()
    aspects = body["aspects"]
    assert isinstance(aspects, dict)
    del aspects[key]
    response = TestClient(identity_app).post(
        "/characters/char-a/personality-profiles/author-seed", json=body
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid identity value"}


@pytest.mark.parametrize("location", ["aspects", "body"])
def test_seed_rejects_unknown_keys(identity_app: FastAPI, location: str) -> None:
    body = seed_body()
    if location == "aspects":
        aspects = body["aspects"]
        assert isinstance(aspects, dict)
        aspects["Humor"] = 0.1
    else:
        body["description"] = "作者が送った未生成の文"
    response = TestClient(identity_app).post(
        "/characters/char-a/personality-profiles/author-seed", json=body
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid identity value"}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("core-identities", {"values": [""], "prohibitions": []}),
        ("core-identities", {"values": [], "prohibitions": [" "]}),
        ("core-identities", {"values": [1], "prohibitions": []}),
        ("temperaments", {"description": ""}),
        ("temperaments", {"description": " \t "}),
        ("temperaments", {"description": 1}),
        ("personality-profiles/author-seed", {**seed_body(), "renderer_version": ""}),
    ],
)
def test_identity_rejects_empty_or_wrong_type_text(
    identity_app: FastAPI, path: str, body: dict[str, object]
) -> None:
    response = TestClient(identity_app).post(f"/characters/char-a/{path}", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("core-identities", {"values": [], "prohibitions": []}),
        ("temperaments", {"description": "穏やかな架空の気質"}),
        ("personality-profiles/author-seed", seed_body()),
    ],
)
@pytest.mark.parametrize("key", ["author_id", "approved", "extra"])
def test_identity_rejects_authority_and_extra_fields(
    identity_app: FastAPI, path: str, body: dict[str, object], key: str
) -> None:
    response = TestClient(identity_app).post(
        f"/characters/char-a/{path}", json={**body, key: "synthetic"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("character", ["char.a", "日本語", "char a"])
def test_identity_rejects_invalid_character_id(identity_app: FastAPI, character: str) -> None:
    client = TestClient(identity_app)
    response = client.post(f"/characters/{character}/temperaments", json={"description": "穏やか"})
    assert response.status_code == 422
    assert client.get(f"/characters/{character}/identity/current").status_code == 422


def test_unconfigured_gateway_returns_503(identity_app: FastAPI) -> None:
    response = TestClient(identity_app).post(
        "/characters/char-a/personality-profiles/author-seed", json=seed_body()
    )
    assert response.status_code == 503
