"""Author deletion contracts through real HTTP validation and selection services."""

from __future__ import annotations

import asyncio
import hmac
import importlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from tests.ut.test_delivery_scope import NOW, Clock
from tests.ut.test_evidence_http import MemoryStore, session_body, turn_body

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.deletion.domain import DeletionRequest, DeletionStatus

pytestmark = pytest.mark.ut
AUTH = {"Authorization": "Bearer synthetic-deletion-token"}


@dataclass
class DeletionStore:
    values: dict[str, DeletionRequest] = field(default_factory=dict)
    blocks: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict)
    error: Exception | None = None

    def get(self, request_id: str) -> DeletionRequest | None:
        if self.error:
            raise self.error
        return self.values.get(request_id)

    def add(self, request: DeletionRequest, blocks: frozenset[tuple[str, str]]) -> DeletionRequest:
        if self.error:
            raise self.error
        if request.request_id not in self.values:
            self.values[request.request_id] = request
            self.blocks[request.request_id] = blocks
        return self.values[request.request_id]

    def save_transition(
        self, previous_status: DeletionStatus, updated: DeletionRequest
    ) -> DeletionRequest:
        if self.values[updated.request_id].status != previous_status:
            ports = importlib.import_module("digital_souls_memory_hindsight.deletion.ports")
            raise ports.DeletionTransitionRejected()
        self.values[updated.request_id] = updated
        return updated

    def blocked_sessions(self, character_id: str) -> frozenset[str]:
        if self.error:
            raise self.error
        return frozenset(
            session
            for key, blocks in self.blocks.items()
            if self.values[key].status != "completed"
            for character, session in blocks
            if character == character_id
        )


def application(store: DeletionStore, *, configured: bool = True) -> FastAPI:
    service = importlib.import_module("digital_souls_memory_hindsight.deletion.service")
    http = importlib.import_module("digital_souls_memory_hindsight.deletion_http")
    evidence = EvidenceService(MemoryStore(), frozenset({"char-a", "char-b", "char-c"}))
    clock = Clock()
    instance = service.DeletionService(store, evidence, clock=clock)
    author = http.AuthorCredentials("synthetic-author", "synthetic-deletion-token")
    app = FastAPI()
    app.state.deletion_service = instance
    app.state.clock = clock
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(http.deletion_router(lambda: instance, author if configured else None))
    return app


def seed(client: TestClient) -> None:
    assert client.post("/sessions", json=session_body()).status_code == 201
    assert client.post("/turns", json=turn_body()).status_code == 201


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize("header", [None, "Bearer synthetic-wrong", "Basic x", "Bearer"])
def test_only_author_can_create_or_read_requests(method: str, header: str | None) -> None:
    store = DeletionStore()
    with TestClient(application(store)) as client:
        response = client.request(
            method,
            "/deletion-requests" + ("/absent" if method == "GET" else ""),
            headers={} if header is None else {"Authorization": header},
            json={"invalid": True},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "Author required"}
    assert store.values == {}
    assert store.blocks == {}


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_missing_author_configuration_is_503_before_body_validation(method: str) -> None:
    with TestClient(application(DeletionStore(), configured=False)) as client:
        response = client.request(
            method,
            "/deletion-requests" + ("/absent" if method == "GET" else ""),
            headers=AUTH,
            content="{invalid json",
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Deletion unavailable"}


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "d"},
        {"request_id": "d", "user_id": "user", "target_turn_ids": ["t-1"]},
        {"request_id": "d", "target_turn_ids": ["t-1"], "extra": True},
        {"target_turn_ids": ["t-1"]},
        {"request_id": "", "target_turn_ids": ["t-1"]},
        {"request_id": " \t", "target_turn_ids": ["t-1"]},
        {"request_id": 1, "target_turn_ids": ["t-1"]},
        {"request_id": "d", "user_id": ""},
        {"request_id": "d", "user_id": " \t"},
        {"request_id": "d", "user_id": 1},
        {"request_id": "d", "target_turn_ids": [""]},
        {"request_id": "d", "target_turn_ids": [" \t"]},
        {"request_id": "d", "target_turn_ids": [1]},
        {"request_id": "d", "target_turn_ids": []},
        {"request_id": "d", "target_turn_ids": "t-1"},
    ],
)
def test_invalid_selection_is_422_without_saving(body: dict[str, object]) -> None:
    store = DeletionStore()
    with TestClient(application(store)) as client:
        response = client.post("/deletion-requests", headers=AUTH, json=body)
    assert response.status_code == 422
    assert store.values == {}
    assert store.blocks == {}


def test_accept_get_and_normalized_replay_preserve_public_response() -> None:
    store = DeletionStore()
    app = application(store)
    with TestClient(app) as client:
        seed(client)
        created = client.post(
            "/deletion-requests",
            headers=AUTH,
            json={"request_id": "d", "target_turn_ids": ["t-1", "t-1"]},
        )
        assert created.status_code == 202, created.text
        body = created.json()
        assert set(body) == {
            "request_id",
            "requested_by",
            "target_turn_ids",
            "status",
            "cascade_result",
            "error",
            "requested_at",
            "completed_at",
        }
        assert body == {
            "request_id": "d",
            "requested_by": "synthetic-author",
            "target_turn_ids": ["t-1"],
            "status": "accepted",
            "cascade_result": None,
            "error": None,
            "requested_at": body["requested_at"],
            "completed_at": None,
        }
        assert datetime.fromisoformat(body["requested_at"]) == NOW
        assert client.get("/deletion-requests/d", headers=AUTH).json() == body
        app.state.clock.now += timedelta(seconds=5)
        replay = client.post(
            "/deletion-requests",
            headers=AUTH,
            json={"request_id": "d", "target_turn_ids": ["t-1"]},
        )
        assert replay.status_code == 202
        assert replay.json() == body
    assert len(store.values) == 1


def test_turn_selection_is_sorted_and_missing_target_does_not_save() -> None:
    store = DeletionStore()
    with TestClient(application(store)) as client:
        seed(client)
        assert client.post("/turns", json=turn_body(turn_id="a")).status_code == 201
        accepted = client.post(
            "/deletion-requests",
            headers=AUTH,
            json={"request_id": "d", "target_turn_ids": ["t-1", "a", "t-1"]},
        )
        assert accepted.status_code == 202
        assert accepted.json()["target_turn_ids"] == ["a", "t-1"]
        replay = client.post(
            "/deletion-requests",
            headers=AUTH,
            json={"request_id": "d", "target_turn_ids": ["a", "t-1"]},
        )
        assert replay.status_code == 202
        assert replay.json() == accepted.json()
        rejected = client.post(
            "/deletion-requests",
            headers=AUTH,
            json={"request_id": "missing", "target_turn_ids": ["t-1", "absent"]},
        )
        assert rejected.status_code == 422
        assert client.get("/deletion-requests/missing", headers=AUTH).status_code == 404
    assert set(store.values) == set(store.blocks) == {"d"}


def test_user_selection_is_union_and_snapshot_does_not_expand_on_replay() -> None:
    store = DeletionStore()
    with TestClient(application(store)) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        for identifier, actor, addressees in (
            ("z-actor", "user", []),
            ("a-addressed", "char-a", ["user"]),
            ("both", "user", ["user"]),
            ("unrelated", "char-a", []),
        ):
            response = client.post(
                "/turns",
                json=turn_body(
                    turn_id=identifier,
                    actor=actor,
                    addressees=addressees,
                    perceived_by=["char-a"],
                    address_mode="direct" if addressees else "broadcast",
                ),
            )
            assert response.status_code == 201, response.text
        body = {"request_id": "d", "user_id": "user"}
        first = client.post("/deletion-requests", headers=AUTH, json=body)
        assert first.status_code == 202, first.text
        assert first.json()["target_turn_ids"] == ["a-addressed", "both", "z-actor"]
        assert (
            client.post("/turns", json=turn_body(turn_id="later", actor="user")).status_code == 201
        )
        again = client.post("/deletion-requests", headers=AUTH, json=body)
        assert again.status_code == 202
        assert again.json() == first.json()
        empty = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "empty", "user_id": "nobody"}
        )
        assert empty.status_code == 202
        assert empty.json()["target_turn_ids"] == []


@pytest.mark.parametrize(
    "original,replacement",
    [
        ({"target_turn_ids": ["t-1"]}, {"target_turn_ids": ["other"]}),
        ({"target_turn_ids": ["t-1"]}, {"user_id": "char-a"}),
        ({"user_id": "char-a"}, {"target_turn_ids": ["t-1"]}),
        ({"user_id": "char-a"}, {"user_id": "other-user"}),
    ],
)
def test_conflicting_replay_never_overwrites_request(
    original: dict[str, object], replacement: dict[str, object]
) -> None:
    with TestClient(application(DeletionStore())) as client:
        seed(client)
        first = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", **original}
        )
        assert first.status_code == 202
        conflict = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", **replacement}
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "Deletion conflict"}
        assert client.get("/deletion-requests/d", headers=AUTH).json() == first.json()


def test_replay_returns_current_status_and_original_time() -> None:
    app = application(DeletionStore())
    with TestClient(app) as client:
        seed(client)
        body = {"request_id": "d", "target_turn_ids": ["t-1"]}
        first = client.post("/deletion-requests", headers=AUTH, json=body)
        assert first.status_code == 202
        app.state.deletion_service.transition("d", "processing")
        again = client.post("/deletion-requests", headers=AUTH, json=body)
        assert again.status_code == 202
        assert again.json()["status"] == "processing"
        assert again.json()["requested_at"] == first.json()["requested_at"]


@pytest.mark.parametrize("same", [False, True])
def test_acceptance_compares_request_inserted_after_initial_lookup(same: bool) -> None:
    class RacingStore(DeletionStore):
        def add(
            self, request: DeletionRequest, blocks: frozenset[tuple[str, str]]
        ) -> DeletionRequest:
            winner = replace(
                request,
                user_id=request.user_id if same else "other-user",
                requested_at=NOW - timedelta(seconds=1),
            )
            return super().add(winner, blocks)

    with TestClient(application(RacingStore())) as client:
        response = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", "user_id": "nobody"}
        )
        assert response.status_code == (202 if same else 409)
        saved = client.get("/deletion-requests/d", headers=AUTH)
        assert saved.status_code == 200
        assert datetime.fromisoformat(saved.json()["requested_at"]) == NOW - timedelta(seconds=1)
        if same:
            assert response.json() == saved.json()
        else:
            assert response.json() == {"detail": "Deletion conflict"}


def test_storage_failure_is_sanitized_and_next_request_can_succeed() -> None:
    store = DeletionStore(error=EvidenceUnavailable("synthetic private database detail"))
    with TestClient(application(store)) as client:
        failed = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", "user_id": "nobody"}
        )
        assert failed.status_code == 503
        assert failed.json() == {"detail": "Deletion unavailable"}
        assert store.values == {}
        store.error = None
        success = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", "user_id": "nobody"}
        )
        assert success.status_code == 202


def test_author_token_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    comparison = MagicMock(wraps=hmac.compare_digest)
    monkeypatch.setattr(hmac, "compare_digest", comparison)
    with TestClient(application(DeletionStore())) as client:
        response = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", "user_id": "nobody"}
        )
        assert response.status_code == 202
    assert comparison.called
    assert any(
        call.args
        in (
            (b"synthetic-deletion-token", b"synthetic-deletion-token"),
            ("synthetic-deletion-token", "synthetic-deletion-token"),
        )
        for call in comparison.call_args_list
    )


def test_production_app_loads_author_and_exposes_both_deletion_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.ut.test_anchor_http import AnchorStore

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a"]')
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "configured-synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-deletion-token")
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    module = importlib.reload(importlib.import_module("digital_souls_memory_hindsight.app"))
    monkeypatch.setattr(module, "PostgresDeletionStore", MagicMock(return_value=DeletionStore()))
    monkeypatch.setattr(module, "PostgresAnchorStore", MagicMock(return_value=AnchorStore()))
    monkeypatch.setattr(
        module, "ledger_service", EvidenceService(MemoryStore(), frozenset({"char-a"}))
    )
    worker = MagicMock()
    worker.run = AsyncMock()
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=worker))
    with TestClient(module.app) as client:
        seed(client)
        denied = client.get("/deletion-requests/d")
        assert denied.status_code == 401
        accepted = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", "target_turn_ids": ["t-1"]}
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["requested_by"] == "configured-synthetic-author"
        assert client.get("/deletion-requests/d", headers=AUTH).json() == accepted.json()


@pytest.mark.parametrize("missing", ["MEMORY_AUTHOR_ID", "MEMORY_AUTHOR_TOKEN"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_production_routes_reject_missing_author_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str, method: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-deletion-token")
    monkeypatch.delenv(missing)
    module = importlib.reload(importlib.import_module("digital_souls_memory_hindsight.app"))

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app), base_url="http://service"
        ) as client:
            response = await client.request(
                method,
                "/deletion-requests" + ("/absent" if method == "GET" else ""),
                headers=AUTH,
                json={"request_id": "d", "user_id": "nobody"},
            )
        assert response.status_code == 503
        assert response.json() == {"detail": "Deletion unavailable"}

    asyncio.run(exercise())
