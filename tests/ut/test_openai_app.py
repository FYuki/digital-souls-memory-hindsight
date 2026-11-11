"""Environment wiring reaches registration consumers without network or PostgreSQL."""

import importlib
import json
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.voice.service import VoiceService
from tests.consistency_doubles import Gateway, Reviewer
from tests.ut.test_evidence_http import MemoryStore
from tests.ut.test_identity_http import seed_body
from tests.ut.test_identity_service import IdentityStore
from tests.ut.test_openai_compatible import STATE, completion, gateway
from tests.ut.test_openai_settings import LLM_ENV
from tests.ut.test_voice_http import BASE, DeletionState, VoiceStore, voice_body

pytestmark = pytest.mark.ut
ROOT = "/characters/char-a"


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> IdentityStore:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    for key in LLM_ENV:
        monkeypatch.delenv(key, raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    store = IdentityStore()
    monkeypatch.setattr(module, "PostgresIdentityStore", lambda url: store)
    return store


def client_for(app: FastAPI, store: IdentityStore) -> tuple[TestClient, VoiceStore]:
    app.state.ledger_service = EvidenceService(MemoryStore(), frozenset({"char-a"}))
    voices = VoiceStore()
    app.state.voice_service = VoiceService(
        voices,
        app.state.ledger_service,
        DeletionState(),
        store,
        app.state.llm_gateway,
        app.state.reviewer,
    )
    return TestClient(app), voices


@pytest.mark.parametrize("configured", [False, True])
def test_environment_controls_real_identity_and_voice_operations(
    environment: IdentityStore, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    requests: list[httpx.Request] = []

    def handle(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        system = json.loads(request.content)["messages"][0]["content"]
        content = (
            {"consistent": True, "reason": "HTTP から届いた架空の審査理由"}
            if "review" in system.split("]", 1)[0]
            else {"description": "HTTP から届いた架空の人格", "response": "HTTP の試作発話"}
        )
        return completion(json.dumps(content))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    if configured:
        for key, value in LLM_ENV.items():
            monkeypatch.setenv(key, value)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    app = cast(FastAPI, module.create_app())
    client, voices = client_for(app, environment)
    assert client.post(ROOT + "/temperaments", json={"description": "穏やか"}).status_code == 201
    seeded = client.post(ROOT + "/personality-profiles/author-seed", json=seed_body())
    voice = client.post(BASE, json=voice_body())
    assert seeded.status_code == voice.status_code == (201 if configured else 503)
    if configured:
        assert seeded.json()["description"] == "HTTP から届いた架空の人格"
        assert (
            seeded.json()["review"]
            == voice.json()["consistency_review"]
            == {
                "consistent": True,
                "reason": "HTTP から届いた架空の審査理由",
            }
        )
        assert app.state.context_gateway is not None
        assert len(requests) == 4
        for request in requests:
            assert str(request.url) == LLM_ENV["MEMORY_LLM_BASE_URL"] + "/chat/completions"
            assert request.headers["authorization"] == "Bearer " + LLM_ENV["MEMORY_LLM_API_KEY"]
            assert json.loads(request.content)["model"] == LLM_ENV["MEMORY_LLM_MODEL"]
            assert request.extensions["timeout"]["read"] == 7.5
    else:
        assert app.state.context_gateway is None
        assert environment.current("char-a").personality_profile is None
        assert voices.values == {}
        assert requests == []


@pytest.mark.parametrize("mask", range(1, 8))
def test_explicit_ports_override_environment_and_uninjected_ports_still_work(
    environment: IdentityStore, monkeypatch: pytest.MonkeyPatch, mask: int
) -> None:
    import asyncio

    def handle(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        return completion(
            json.dumps(
                {"consistent": True, "reason": "環境側の審査理由"}
                if "review" in system.split("]", 1)[0]
                else {"description": "環境側の人格"}
            )
        )

    async def handle_async(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return completion(json.dumps(STATE))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)
    for key, value in LLM_ENV.items():
        monkeypatch.setenv(key, value)
    injected_gateway, injected_reviewer = Gateway(), Reviewer(reason="注入側の審査理由")
    injected_context = MagicMock(generate_state=AsyncMock(return_value={**STATE, "mood": "注入側"}))
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    app = module.create_app(
        llm_gateway=injected_gateway if mask & 1 else None,
        reviewer=injected_reviewer if mask & 2 else None,
        context_gateway=injected_context if mask & 4 else None,
    )
    client, _ = client_for(app, environment)
    assert client.post(ROOT + "/temperaments", json={"description": "穏やか"}).status_code == 201
    seeded = client.post(ROOT + "/personality-profiles/author-seed", json=seed_body())
    assert seeded.status_code == 201
    assert seeded.json()["description"] == ("架空の人格の記述" if mask & 1 else "環境側の人格")
    assert seeded.json()["review"]["reason"] == (
        "注入側の審査理由" if mask & 2 else "環境側の審査理由"
    )
    result = asyncio.run(app.state.context_gateway.generate_state("char-a", "s-1", "Web", ()))
    assert result == {**STATE, "mood": "注入側" if mask & 4 else STATE["mood"]}


@pytest.mark.parametrize("operation", ["seed", "voice"])
@pytest.mark.parametrize("stage", ["generate", "review"])
@pytest.mark.parametrize("failure", ["json", "missing", "blank", "timeout", "http"])
def test_adapter_failures_return_503_without_registration_and_allow_retry(
    environment: IdentityStore, operation: str, stage: str, failure: str
) -> None:
    failing = True

    def handle(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        reviewing = "review" in system.split("]", 1)[0]
        if failing and reviewing == (stage == "review"):
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if failure == "http":
                return httpx.Response(500)
            content = {
                "json": "broken",
                "missing": "{}",
                "blank": json.dumps(
                    {"description": "", "response": "", "consistent": True, "reason": ""}
                ),
            }[failure]
            return completion(content)
        return completion(
            json.dumps(
                {"consistent": True, "reason": "再試行の審査理由"}
                if reviewing
                else {"description": "架空の人格", "response": "架空の発話"}
            )
        )

    port = gateway(handle)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    client, voices = client_for(module.create_app(llm_gateway=port, reviewer=port), environment)
    assert client.post(ROOT + "/temperaments", json={"description": "穏やか"}).status_code == 201
    path, body = (
        (ROOT + "/personality-profiles/author-seed", seed_body())
        if operation == "seed"
        else (BASE, voice_body())
    )
    before = client.get(ROOT + "/identity/current").json()
    assert client.post(path, json=body).status_code == 503
    assert client.get(ROOT + "/identity/current").json() == before
    assert voices.values == {}
    assert voices.reviews == {}
    failing = False
    recovered = client.post(path, json=body)
    assert recovered.status_code == 201
    review_field = "review" if operation == "seed" else "consistency_review"
    assert recovered.json()[review_field] == {"consistent": True, "reason": "再試行の審査理由"}


def test_real_reviewer_inconsistency_preserves_422_contract(environment: IdentityStore) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        return completion(
            json.dumps(
                {"consistent": False, "reason": "架空の矛盾理由", "aspects": {"Openness": 0.5}}
                if "review" in system.split("]", 1)[0]
                else {"description": "架空の人格"}
            )
        )

    port = gateway(handle)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    client, _ = client_for(module.create_app(llm_gateway=port, reviewer=port), environment)
    assert client.post(ROOT + "/temperaments", json={"description": "穏やか"}).status_code == 201
    response = client.post(ROOT + "/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 422
    assert response.json() == {
        "detail": "Identity inconsistent",
        "consistency_review": {"consistent": False, "reason": "架空の矛盾理由"},
    }
    assert environment.current("char-a").personality_profile is None
