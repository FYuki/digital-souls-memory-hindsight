"""Registration consistency decisions at HTTP boundaries with synthetic ports."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Barrier
from typing import cast
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.identity.ports import IdentityUnavailable
from tests.consistency_doubles import Gateway, Reviewer
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import MemoryStore
from tests.ut.test_identity_http import seed_body
from tests.ut.test_identity_service import IdentityStore
from tests.ut.test_voice_http import BASE, DeletionState, VoiceStore, voice_body

pytestmark = pytest.mark.ut
ROOT = "/characters/char-a"


@pytest.fixture
def identity_store() -> IdentityStore:
    return IdentityStore()


@pytest.fixture
def reviewer() -> Reviewer:
    return Reviewer()


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()


@pytest.fixture
def environment(
    identity_store: IdentityStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a", "char-b"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_ID", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_TOKEN", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    monkeypatch.setattr(module, "PostgresIdentityStore", lambda url: identity_store)


@pytest.fixture
def client(
    environment: None, identity_store: IdentityStore, gateway: Gateway, reviewer: Reviewer
) -> TestClient:
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    app = module.create_app(llm_gateway=gateway, reviewer=reviewer)
    app.state.ledger_service = EvidenceService(MemoryStore(), frozenset({"char-a", "char-b"}))
    app.state.test_voice_store = VoiceStore()
    app.state.test_identity_store = identity_store
    result = TestClient(app)
    configure_voice(result, gateway, reviewer)
    return result


def configure_voice(client: TestClient, gateway: Gateway | None, reviewer: Reviewer | None) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.voice.service")
    state = cast(FastAPI, client.app).state
    state.voice_service = module.VoiceService(
        state.test_voice_store,
        state.ledger_service,
        DeletionState(),
        state.test_identity_store,
        gateway,
        reviewer,
        clock=Clock(),
    )


def seed(client: TestClient) -> None:
    assert client.post(ROOT + "/temperaments", json={"description": "穏やか"}).status_code == 201
    response = client.post(ROOT + "/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 201, response.text


def test_seed_with_gateway_but_without_reviewer_is_503_and_unpublished(environment: None) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    client = TestClient(module.create_app(llm_gateway=Gateway()))
    assert client.post(ROOT + "/temperaments", json={"description": "気質"}).status_code == 201
    response = client.post(ROOT + "/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 503
    assert client.get(ROOT + "/identity/current").json()["personality_profile"] is None


def test_invalid_voice_does_not_consume_the_first_review(
    client: TestClient, gateway: Gateway, reviewer: Reviewer
) -> None:
    assert client.post(BASE, json=voice_body(approved_by="")).status_code == 422
    assert gateway.voice_calls == []
    assert reviewer.voice_calls == []
    response = client.post(BASE, json=voice_body())
    assert response.status_code == 201
    assert response.json()["consistency_review"] is not None


def test_seed_reviews_rendered_description_with_latest_temperament(
    client: TestClient, reviewer: Reviewer
) -> None:
    for description in ("古い気質", "最新の気質"):
        assert (
            client.post(ROOT + "/temperaments", json={"description": description}).status_code
            == 201
        )
    response = client.post(ROOT + "/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 201
    assert reviewer.personality_calls == [("最新の気質", response.json()["description"])]
    assert response.json()["review"] == {"consistent": True, "reason": reviewer.reason}


def test_temperament_reviews_new_description_against_existing_profile(
    client: TestClient, reviewer: Reviewer
) -> None:
    seed(client)
    profile = client.get(ROOT + "/identity/current").json()["personality_profile"]
    reviewer.personality_calls.clear()
    reviewer.reason = "更新された気質についての別の審査理由"
    response = client.post(ROOT + "/temperaments", json={"description": "新しい気質"})
    assert response.status_code == 201
    assert reviewer.personality_calls == [("新しい気質", profile["description"])]
    assert response.json()["review"] == {"consistent": True, "reason": reviewer.reason}
    assert "review" not in client.get(ROOT + "/temperaments/2").json()


def test_core_registration_with_profile_does_not_review(
    client: TestClient, reviewer: Reviewer
) -> None:
    seed(client)
    reviewer.personality_calls.clear()
    reviewer.error = RuntimeError("core must not be reviewed")
    response = client.post(ROOT + "/core-identities", json={"values": [], "prohibitions": []})
    assert response.status_code == 201
    assert reviewer.personality_calls == []


def test_core_and_profileless_temperament_do_not_require_or_call_reviewer(
    client: TestClient, reviewer: Reviewer
) -> None:
    reviewer.error = RuntimeError("must not review")
    for path, body in (
        ("core-identities", {"values": [], "prohibitions": []}),
        ("temperaments", {"description": "未審査の気質"}),
    ):
        response = client.post(ROOT + "/" + path, json=body)
        assert response.status_code == 201
        if path == "temperaments":
            assert response.json()["review"] is None
    assert reviewer.personality_calls == []
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    unconfigured = TestClient(module.create_app())
    response = unconfigured.post(ROOT + "/temperaments", json={"description": "気質"})
    assert response.status_code == 201
    assert response.json()["review"] is None
    assert (
        unconfigured.post(
            ROOT + "/core-identities", json={"values": [], "prohibitions": []}
        ).status_code
        == 201
    )


@pytest.mark.parametrize("operation", ["seed", "temperament"])
@pytest.mark.parametrize(
    "failure", ["inconsistent", "exception", "shape", "bool", "reason", "missing"]
)
def test_identity_rejection_preserves_current_and_version_for_retry(
    client: TestClient, reviewer: Reviewer, operation: str, failure: str
) -> None:
    if operation == "temperament":
        seed(client)
    else:
        assert (
            client.post(ROOT + "/temperaments", json={"description": "穏やか"}).status_code == 201
        )
    before = client.get(ROOT + "/identity/current").json()
    path, body, version = (
        ("personality-profiles/author-seed", seed_body(), 1)
        if operation == "seed"
        else ("temperaments", {"description": "候補の気質"}, 2)
    )
    reviewer.consistent = failure != "inconsistent"
    reviewer.error = RuntimeError("synthetic review failure") if failure == "exception" else None
    reviewer.invalid = failure if failure in {"shape", "bool", "reason"} else None
    failing = client
    if failure == "missing":
        module = importlib.import_module("digital_souls_memory_hindsight.app")
        failing = TestClient(module.create_app(llm_gateway=Gateway()))
    response = failing.post(ROOT + "/" + path, json=body)
    assert response.status_code == (422 if failure == "inconsistent" else 503)
    if failure == "inconsistent":
        assert set(response.json()) == {"detail", "consistency_review"}
        assert response.json()["consistency_review"] == {
            "consistent": False,
            "reason": reviewer.reason,
        }
    assert client.get(ROOT + "/identity/current").json() == before
    get_path = "personality-profiles" if operation == "seed" else "temperaments"
    assert client.get(f"{ROOT}/{get_path}/{version}").status_code == 404
    reviewer.consistent, reviewer.error, reviewer.invalid = True, None, None
    recovered = client.post(ROOT + "/" + path, json=body)
    assert recovered.status_code == 201
    assert recovered.json()["version"] == version


@pytest.mark.parametrize("consistent", [True, False])
@pytest.mark.parametrize("with_temperament", [True, False])
def test_first_voice_returns_review_even_if_inconsistent_and_later_voice_skips_ports(
    client: TestClient,
    gateway: Gateway,
    reviewer: Reviewer,
    consistent: bool,
    with_temperament: bool,
) -> None:
    if with_temperament:
        assert (
            client.post(ROOT + "/temperaments", json={"description": "最新の気質"}).status_code
            == 201
        )
    reviewer.consistent = consistent
    body = voice_body(question="架空の質問")
    first = client.post(BASE, json=body)
    assert first.status_code == 201
    assert first.json()["consistency_review"] == {
        "consistent": consistent,
        "reason": reviewer.reason,
    }
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1
    situation, answer, question, temperament = gateway.voice_calls[0]
    assert situation.strip()
    assert (answer, question, temperament) == (
        body["answer"],
        body["question"],
        "最新の気質" if with_temperament else None,
    )
    assert reviewer.voice_calls == [(situation, gateway.response, answer, question, temperament)]
    saved = client.get(BASE + "/voice-1").json()
    aspects = cast(dict[str, float], seed_body()["aspects"])
    assert not ({"aspects"} | set(aspects)) & set(first.json())
    assert saved == {k: v for k, v in first.json().items() if k != "consistency_review"}
    assert client.get(BASE).json() == [saved]
    gateway.generation_error = reviewer.error = RuntimeError("must not call again")
    second = client.post(BASE, json=voice_body(exemplar_id="second"))
    assert second.status_code == 201
    assert second.json()["consistency_review"] is None
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1
    assert len(client.get(BASE).json()) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "gateway_missing",
        "reviewer_missing",
        "generate",
        "blank",
        "review",
        "shape",
        "bool",
        "reason",
        "identity",
        "store",
    ],
)
def test_failed_first_voice_saves_nothing_and_retry_still_returns_review(
    client: TestClient, gateway: Gateway, reviewer: Reviewer, failure: str
) -> None:
    store = cast(FastAPI, client.app).state.test_voice_store
    configure_voice(
        client,
        None if failure == "gateway_missing" else gateway,
        None if failure == "reviewer_missing" else reviewer,
    )
    gateway.generation_error = (
        RuntimeError("synthetic generation failure") if failure == "generate" else None
    )
    gateway.response = " \t " if failure == "blank" else "架空の応答"
    reviewer.error = RuntimeError("synthetic review failure") if failure == "review" else None
    reviewer.invalid = failure if failure in {"shape", "bool", "reason"} else None
    with (
        patch.object(IdentityStore, "current", side_effect=IdentityUnavailable())
        if failure == "identity"
        else nullcontext()
    ):
        if failure == "store":
            store.error = EvidenceUnavailable()
        failed = client.post(BASE, json=voice_body())
    assert failed.status_code == 503
    store.error = None
    assert client.get(BASE).json() == []
    assert store.reviews == {}
    configure_voice(client, gateway, reviewer)
    gateway.generation_error, gateway.response = None, "架空の応答"
    reviewer.error, reviewer.invalid = None, None
    recovered = client.post(BASE, json=voice_body())
    assert recovered.status_code == 201
    assert recovered.json()["consistency_review"] == {"consistent": True, "reason": reviewer.reason}


def test_reviewed_character_needs_no_ports_but_other_character_still_does(
    client: TestClient,
) -> None:
    assert client.post(BASE, json=voice_body()).status_code == 201
    configure_voice(client, None, None)
    response = client.post(BASE, json=voice_body(exemplar_id="second"))
    assert response.status_code == 201
    assert response.json()["consistency_review"] is None
    assert client.post(BASE.replace("char-a", "char-b"), json=voice_body()).status_code == 503


def test_removing_all_voices_does_not_reset_character_review(
    client: TestClient, gateway: Gateway, reviewer: Reviewer
) -> None:
    assert client.post(BASE, json=voice_body()).status_code == 201
    cast(FastAPI, client.app).state.test_voice_store.values.clear()
    assert client.get(BASE).json() == []
    gateway.generation_error = reviewer.error = RuntimeError("must not review after deletion")
    response = client.post(BASE, json=voice_body(exemplar_id="replacement"))
    assert response.status_code == 201
    assert response.json()["consistency_review"] is None
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1


def test_concurrent_first_voices_return_only_one_committed_review(
    client: TestClient, gateway: Gateway
) -> None:
    barrier = Barrier(2)
    original = gateway.respond_in_voice

    def generate(situation: str, answer: str, question: str | None, temperament: str | None) -> str:
        barrier.wait(timeout=10)
        return original(situation, answer, question, temperament)

    with (
        patch.object(gateway, "respond_in_voice", side_effect=generate),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        pending = [
            pool.submit(client.post, BASE, json=voice_body(exemplar_id=name))
            for name in ("one", "two")
        ]
        responses = [future.result(timeout=15) for future in pending]
    assert [r.status_code for r in responses] == [201, 201]
    assert sum(r.json()["consistency_review"] is not None for r in responses) == 1
    assert len(client.get(BASE).json()) == 2
    assert len(cast(FastAPI, client.app).state.test_voice_store.reviews) == 1
