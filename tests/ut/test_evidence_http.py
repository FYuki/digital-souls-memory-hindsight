"""Evidence rules through the HTTP adapter and real service, with storage isolated.

The test store only retains values and arbitrates duplicate keys. It deliberately
does not validate turns, infer characters, or enforce receipt eligibility.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.domain import (
    DeliveryReceipt,
    Session,
    SessionEnding,
    Turn,
)
from digital_souls_memory_hindsight.evidence.ports import EvidenceConflict, EvidenceStore
from digital_souls_memory_hindsight.outbox import RetainOperation

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.evidence.delivery import DeliveryScope

pytestmark = pytest.mark.ut
STAMP = "2026-10-28T00:00:00Z"


def session_body(**changes: object) -> dict[str, object]:
    return {
        "session_id": "s-1",
        "started_at": STAMP,
        "surface": "Web",
        "reports_delivery": True,
        **changes,
    }


def turn_body(**changes: object) -> dict[str, object]:
    return {
        "turn_id": "t-1",
        "session_id": "s-1",
        "surface": "Web",
        "actor": "char-a",
        "kind": "utterance",
        "text": "  架空のお茶🍵。\n",
        "perceived_by": ["char-a"],
        "addressees": [],
        "address_mode": "broadcast",
        "reply_to_turn_id": None,
        "language": "ja",
        "register": {"一人称": "私"},
        "sensitivity": "public",
        "classifier_version": "fixture-1",
        "occurred_at": STAMP,
        **changes,
    }


def receipt_body(**changes: object) -> dict[str, object]:
    return {
        "status": "none",
        "spoken_until": 0,
        "reported_by": "Web",
        "reported_at": STAMP,
        **changes,
    }


class MemoryStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.turns: dict[str, Turn] = {}
        self.receipts: list[DeliveryReceipt] = []
        self.endings: dict[str, SessionEnding] = {}
        self.delivery_scopes: list[DeliveryScope] = []
        self.outbox: dict[str, RetainOperation] = {}
        self.tombstones: set[str] = set()

    def is_tombstoned(self, turn_id: str) -> bool:
        return turn_id in self.tombstones

    def lock_session(self, session_id: str) -> None:
        self.get_session(session_id)

    def list_projection_sessions(self, bank_id: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.turns[value.turn_id].session_id
                for value in self.outbox.values()
                if value.bank_id == bank_id
                and value.update_mode == "append"
                and value.turn_id in self.turns
            )
        )

    def list_projection_turns(self, session_id: str, bank_id: str) -> tuple[Turn, ...]:
        return tuple(
            self.turns[value.turn_id]
            for value in self.outbox.values()
            if value.bank_id == bank_id
            and value.update_mode == "append"
            and value.turn_id in self.turns
            and self.turns[value.turn_id].session_id == session_id
        )

    @contextmanager
    def transaction(self) -> Iterator[EvidenceStore]:
        turns, receipts, scopes = dict(self.turns), list(self.receipts), list(self.delivery_scopes)
        outbox = dict(self.outbox)
        try:
            yield self
        except Exception:
            self.turns, self.receipts, self.delivery_scopes = turns, receipts, scopes
            self.outbox = outbox
            raise

    def add_retain(self, value: RetainOperation) -> None:
        previous = self.outbox.get(value.operation_id)
        if previous is not None and previous != value:
            raise EvidenceConflict()
        self.outbox[value.operation_id] = value

    def get_turn_for_update(self, turn_id: str) -> Turn | None:
        return self.get_turn(turn_id)

    def add_delivery_scope(self, value: DeliveryScope) -> DeliveryScope:
        value = replace(value, scope_id=len(self.delivery_scopes) + 1)
        self.delivery_scopes.append(value)
        return value

    def get_delivery_scope(self, turn_id: str) -> DeliveryScope | None:
        return next((v for v in reversed(self.delivery_scopes) if v.turn_id == turn_id), None)

    def list_pending_deliveries(self, cutoff: datetime) -> tuple[Turn, ...]:
        return tuple(
            turn
            for turn in self.turns.values()
            if turn.recorded_at <= cutoff
            and (scope := self.get_delivery_scope(turn.turn_id)) is not None
            and scope.spoken_until is None
        )

    def add_session(self, value: Session) -> Session:
        key = value.session_id
        if key in self.sessions:
            raise EvidenceConflict()
        self.sessions[key] = value
        return value

    def get_session(self, session_id: str) -> Session | None:
        value = self.sessions.get(session_id)
        ending = self.endings.get(session_id)
        if value is not None and ending is not None:
            value = replace(value, ended_at=ending.ended_at)
        return value

    def add_turn(self, value: Turn) -> Turn:
        key = value.turn_id
        previous = self.turns.get(key)
        if previous is not None:
            candidate = replace(value, recorded_at=previous.recorded_at)
            if candidate != previous:
                raise EvidenceConflict()
            return previous
        self.turns[key] = value
        return value

    def get_turn(self, turn_id: str) -> Turn | None:
        return self.turns.get(turn_id)

    def add_receipt(self, value: DeliveryReceipt) -> DeliveryReceipt:
        self.receipts.append(value)
        return value

    def list_receipts(self, turn_id: str) -> tuple[DeliveryReceipt, ...]:
        return tuple(v for v in self.receipts if v.turn_id == turn_id)

    def list_perceived_turns(self, character_id: str) -> tuple[Turn, ...]:
        return tuple(v for v in self.turns.values() if character_id in v.perceived_by)

    def list_user_turns(self, user_id: str) -> tuple[Turn, ...]:
        return tuple(
            sorted(
                (v for v in self.turns.values() if v.actor == user_id or user_id in v.addressees),
                key=lambda v: v.turn_id,
            )
        )

    def add_ending(self, value: SessionEnding) -> tuple[SessionEnding, bool]:
        key = value.session_id
        previous = self.endings.get(key)
        if previous is not None:
            if any(
                getattr(previous, field) != getattr(value, field)
                for field in ("ended_at", "reported_by")
            ):
                raise EvidenceConflict()
            return previous, False
        self.endings[key] = value
        return value, True

    def list_endings(self, session_id: str) -> tuple[SessionEnding, ...]:
        value = self.endings.get(session_id)
        return () if value is None else (value,)


def application(
    character_ids: frozenset[str] | None, store: EvidenceStore | None = None
) -> FastAPI:
    service_module = importlib.import_module("digital_souls_memory_hindsight.evidence.service")
    http_module = importlib.import_module("digital_souls_memory_hindsight.evidence_http")
    service = service_module.EvidenceService(
        store=MemoryStore() if store is None else store, character_ids=character_ids
    )
    app = FastAPI()
    app.include_router(http_module.evidence_router(lambda: service))
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(application(frozenset({"char-a", "char-b"}))) as result:
        yield result


@pytest.mark.parametrize("character_ids", [None, frozenset()])
def test_unavailable_registry_does_not_fall_back_to_an_empty_registry(
    character_ids: frozenset[str] | None,
) -> None:
    with TestClient(application(character_ids)) as client:
        start(client)
        body = turn_body(actor="char-visitor", perceived_by=[])
        del body["address_mode"]
        response = client.post("/turns", json=body)
        if character_ids is None:
            assert response.status_code == 503
            assert response.json() == {"detail": "Evidence unavailable"}
            assert client.get("/turns/t-1").status_code == 404
        else:
            assert response.status_code == 201
            assert response.json()["address_mode"] == "broadcast"


def start(client: TestClient, **changes: object) -> None:
    response = client.post("/sessions", json=session_body(**changes))
    assert response.status_code == 201, response.text


def test_session_roundtrip_preserves_json_inside_surface(client: TestClient) -> None:
    start(client, surface='{"participants":[]}')
    response = client.get("/sessions/s-1")
    assert response.status_code == 200
    assert response.json() == {**session_body(surface='{"participants":[]}'), "ended_at": None}


@pytest.mark.parametrize("key", ["participants", r"partici\u0070ants"])
def test_session_rejects_unknown_top_level_keys(client: TestClient, key: str) -> None:
    raw = json.dumps(session_body())[:-1] + ', "' + key + '": []}'
    response = client.post("/sessions", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert client.get("/sessions/s-1").status_code == 404
    errors = response.json()["detail"]
    assert errors and all(set(error) == {"loc", "type", "msg"} for error in errors)
    assert any(error["loc"] == ["body", "participants"] for error in errors)


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_session_requires_strict_boolean(client: TestClient, value: object) -> None:
    response = client.post("/sessions", json=session_body(reports_delivery=value))
    assert response.status_code == 422
    assert client.get("/sessions/s-1").status_code == 404


def test_session_requires_delivery_declaration(client: TestClient) -> None:
    body = session_body()
    del body["reports_delivery"]
    assert client.post("/sessions", json=body).status_code == 422
    assert client.get("/sessions/s-1").status_code == 404


def test_session_delivery_declaration_is_immutable(client: TestClient) -> None:
    start(client, reports_delivery=False)
    conflict = client.post("/sessions", json=session_body(reports_delivery=True))
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "Evidence already exists"}
    assert client.get("/sessions/s-1").json()["reports_delivery"] is False


def test_turn_roundtrip_and_identical_retry_keep_recorded_time(client: TestClient) -> None:
    start(client)
    before = datetime.now(UTC)
    first = client.post("/turns", json=turn_body())
    assert first.status_code == 201
    saved = first.json()
    recorded = datetime.fromisoformat(saved["recorded_at"].replace("Z", "+00:00"))
    assert before <= recorded <= datetime.now(UTC)
    assert saved == {**turn_body(), "recorded_at": saved["recorded_at"]}
    retry = client.post("/turns", json=turn_body())
    assert retry.status_code == 201
    assert retry.json() == saved
    assert client.get("/turns/t-1").json() == saved
    assert client.get("/characters/char-a/turns").json() == [saved]


@pytest.mark.parametrize(
    ("original_changes", "changes", "new_status"),
    [
        ({}, {"session_id": "absent-session"}, 404),
        ({}, {"reply_to_turn_id": "absent-turn"}, 422),
        ({}, {"perceived_by": ["char-a", "char-a"]}, 422),
        (
            {"address_mode": "direct", "addressees": ["user-a", "user-b"]},
            {"addressees": ["user-a", "user-a"]},
            422,
        ),
        ({}, {"perceived_by": ["char-a", "char-outside"]}, 422),
        ({}, {"perceived_by": ["char-b"]}, 422),
        ({"kind": "thought"}, {"actor": "user-a"}, 422),
        ({"perceived_by": ["char-a", "char-b"]}, {"kind": "thought"}, 422),
        ({}, {"address_mode": None}, 422),
        (
            {"actor": "user-a", "perceived_by": [], "address_mode": None},
            {"addressees": ["user-b"]},
            422,
        ),
        ({"address_mode": "direct", "addressees": ["user-a"]}, {"addressees": []}, 422),
        ({}, {"addressees": ["user-a"]}, 422),
        ({"address_mode": "self"}, {"addressees": ["user-a"]}, 422),
    ],
)
def test_existing_turn_conflicts_before_new_registration_validation(
    original_changes: dict[str, object], changes: dict[str, object], new_status: int
) -> None:
    store = MemoryStore()
    with TestClient(application(frozenset({"char-a", "char-b"}), store)) as client:
        start(client)
        original = turn_body(**original_changes)
        first = client.post("/turns", json=original)
        assert first.status_code == 201
        candidate = {**original, **changes}
        response = client.post("/turns", json=candidate)
        assert response.status_code == 409
        assert response.json() == {"detail": "Evidence already exists"}
        assert client.post("/turns", json={**candidate, "turn_id": "t-2"}).status_code == new_status
        assert client.get("/turns/t-2").status_code == 404
        assert client.get("/turns/t-1").json() == first.json()
        assert len(store.turns) == 1


@pytest.mark.parametrize("omitted", [False, True])
def test_existing_character_turn_rejects_removing_explicit_mode(omitted: bool) -> None:
    store = MemoryStore()
    with TestClient(application(frozenset({"char-a"}), store)) as client:
        start(client)
        original = turn_body()
        first = client.post("/turns", json=original)
        assert first.status_code == 201
        assert client.post("/turns", json=original).json() == first.json()
        candidate = {**original, "address_mode": None}
        if omitted:
            del candidate["address_mode"]
        assert client.post("/turns", json=candidate).status_code == 409
        assert client.post("/turns", json={**candidate, "turn_id": "t-2"}).status_code == 422
        assert client.get("/turns/t-1").json() == first.json()
        assert client.get("/turns/t-2").status_code == 404
        assert len(store.turns) == 1


@pytest.mark.parametrize(
    ("change", "omitted"),
    [("remove-perceiver", False), ("add-actor", False), ("add-actor", True)],
)
def test_retry_uses_saved_content_after_character_configuration_changes(
    change: str, omitted: bool
) -> None:
    store = MemoryStore()
    body = (
        turn_body(perceived_by=["char-a", "char-b"])
        if change == "remove-perceiver"
        else turn_body(actor="user-a", perceived_by=[], address_mode=None)
    )
    if change == "add-actor" and omitted:
        del body["address_mode"]
    with TestClient(application(frozenset({"char-a", "char-b"}), store)) as before:
        start(before)
        first = before.post("/turns", json=body)
        assert first.status_code == 201
    characters = {"char-a"} if change == "remove-perceiver" else {"char-a", "char-b", "user-a"}
    with TestClient(application(frozenset(characters), store)) as after:
        retry = after.post("/turns", json=body)
        assert retry.status_code == 201
        assert retry.json() == first.json()
        assert after.post("/turns", json={**body, "turn_id": "t-2"}).status_code == 422
        assert after.get("/turns/t-2").status_code == 404
        assert after.get("/turns/t-1").json() == first.json()
        assert len(store.turns) == 1


def test_existing_turn_still_requires_valid_http_types(client: TestClient) -> None:
    start(client)
    first = client.post("/turns", json=turn_body())
    assert first.status_code == 201
    assert client.post("/turns", json=turn_body(reply_to_turn_id=["t-1"])).status_code == 422
    assert client.get("/turns/t-1").json() == first.json()


def test_failed_existing_turn_lookup_returns_503_without_inserting() -> None:
    from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable

    class UnavailableStore(MemoryStore):
        def get_turn(self, turn_id: str) -> Turn | None:
            raise EvidenceUnavailable("synthetic read failure")

    store = UnavailableStore()
    with TestClient(application(frozenset({"char-a"}), store)) as client:
        start(client)
        response = client.post("/turns", json=turn_body())
        assert response.status_code == 503
        assert response.json() == {"detail": "Evidence unavailable"}
        assert store.turns == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": "s-2"},
        {"surface": "Windows"},
        {"actor": "char-b"},
        {"kind": "observation"},
        {"text": "異なる本文"},
        {"perceived_by": ["char-a"]},
        {"addressees": ["user-b"]},
        {"address_mode": "self"},
        {"reply_to_turn_id": "parent"},
        {"language": "en"},
        {"register": {"一人称": "僕"}},
        {"sensitivity": "private"},
        {"classifier_version": "fixture-2"},
        {"occurred_at": "2026-10-28T00:00:01Z"},
    ],
)
def test_turn_conflicts_on_each_changed_input(
    client: TestClient, changes: dict[str, object]
) -> None:
    start(client)
    start(client, session_id="s-2")
    assert client.post("/turns", json=turn_body(turn_id="parent")).status_code == 201
    original = turn_body(
        perceived_by=["char-a", "char-b"], address_mode="direct", addressees=["user-a"]
    )
    if "address_mode" in changes:
        original = {**original, "address_mode": "broadcast", "addressees": []}
    saved = client.post("/turns", json=original)
    assert saved.status_code == 201
    response = client.post("/turns", json={**original, **changes})
    assert response.status_code == 409
    assert response.json() == {"detail": "Evidence already exists"}
    assert client.get("/turns/t-1").json() == saved.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unknown"},
        {"kind": "thought", "actor": "user-a", "perceived_by": ["char-a"]},
        {"kind": "thought", "perceived_by": ["char-a", "char-b"]},
        {"kind": "thought", "perceived_by": []},
        {"perceived_by": ["char-b"]},
        {"perceived_by": ["char-a", "char-a"]},
        {"perceived_by": ["char-a", " "]},
        {"perceived_by": ["char-a", "user-a"]},
        {"addressees": ["user-a", "user-a"], "address_mode": "direct"},
        {"addressees": [" "], "address_mode": "direct"},
        {"address_mode": None},
        {"address_mode": "unknown"},
        {"address_mode": "direct"},
        {"address_mode": "broadcast", "addressees": ["user-a"]},
        {"address_mode": "self", "addressees": ["user-a"]},
        {"reply_to_turn_id": "missing"},
        {"reply_to_turn_id": ["parent"]},
    ],
)
def test_turn_rules_reject_without_persisting(
    client: TestClient, changes: dict[str, object]
) -> None:
    start(client)
    response = client.post("/turns", json=turn_body(**changes))
    assert response.status_code == 422
    assert client.get("/turns/t-1").status_code == 404
    assert client.get("/characters/char-a/turns").json() == []


def test_unregistered_actor_cannot_become_character_by_claiming_self_perception(
    client: TestClient,
) -> None:
    start(client)
    response = client.post(
        "/turns",
        json=turn_body(
            actor="char-visitor", kind="thought", perceived_by=["char-visitor"], address_mode="self"
        ),
    )
    assert response.status_code == 422
    assert client.get("/turns/t-1").status_code == 404


def test_character_address_mode_cannot_be_omitted(client: TestClient) -> None:
    start(client)
    body = turn_body()
    del body["address_mode"]
    response = client.post("/turns", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid evidence value"}
    assert client.get("/turns/t-1").status_code == 404


@pytest.mark.parametrize("omitted", [True, False])
@pytest.mark.parametrize("addressees", [[], ["char-b"]])
def test_user_default_address_mode(
    client: TestClient, omitted: bool, addressees: list[str]
) -> None:
    start(client)
    body = turn_body(actor="user-a", perceived_by=[], address_mode=None, addressees=addressees)
    if omitted:
        del body["address_mode"]
    response = client.post("/turns", json=body)
    if addressees:
        assert response.status_code == 422
        assert client.get("/turns/t-1").status_code == 404
    else:
        assert response.status_code == 201
        assert response.json()["address_mode"] == "broadcast"
        assert response.json()["perceived_by"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "observation"},
        {"kind": "thought"},
        {"address_mode": "self", "perceived_by": ["char-a", "char-b"]},
        {"address_mode": "direct", "addressees": ["char-b"]},
        {"actor": "user-a", "address_mode": "direct", "addressees": ["user-b"]},
        {"actor": "user-a", "address_mode": "self", "perceived_by": []},
    ],
)
def test_valid_turn_variants_preserve_perception(
    client: TestClient, changes: dict[str, object]
) -> None:
    start(client)
    body = turn_body(**changes)
    response = client.post("/turns", json=body)
    assert response.status_code == 201
    assert response.json()["address_mode"] == body["address_mode"]
    assert response.json()["perceived_by"] == body["perceived_by"]
    expected = [response.json()] if "char-b" in cast(list[str], body["perceived_by"]) else []
    assert client.get("/characters/char-b/turns").json() == expected


def test_reply_target_can_belong_to_another_session_and_surface(client: TestClient) -> None:
    start(client)
    start(client, session_id="s-2", surface="Windows")
    assert (
        client.post(
            "/turns",
            json=turn_body(
                turn_id="parent",
                session_id="s-2",
                surface="Windows",
                actor="user-a",
                perceived_by=[],
            ),
        ).status_code
        == 201
    )
    response = client.post("/turns", json=turn_body(reply_to_turn_id="parent"))
    assert response.status_code == 201
    assert client.get("/turns/t-1").json()["reply_to_turn_id"] == "parent"


@pytest.mark.parametrize(("reports_delivery", "kind"), [(False, "utterance"), (True, "thought")])
def test_ineligible_receipt_is_not_saved_and_turn_is_unchanged(
    client: TestClient, reports_delivery: bool, kind: str
) -> None:
    start(client, reports_delivery=reports_delivery)
    saved = client.post("/turns", json=turn_body(kind=kind))
    assert saved.status_code == 201
    response = client.post("/turns/t-1/receipts", json=receipt_body())
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid evidence value"}
    assert client.get("/turns/t-1/receipts").json() == []
    assert client.get("/turns/t-1").json() == saved.json()


@pytest.mark.parametrize(
    ("status", "length", "accepted"),
    [
        ("none", 0, True),
        ("full", 3, True),
        ("partial", 1, True),
        ("partial", 2, True),
        ("none", 1, False),
        ("full", 2, False),
        ("full", 4, False),
        ("partial", 0, False),
        ("partial", 3, False),
        ("partial", 4, False),
        ("none", -1, False),
        ("none", True, False),
        ("none", "0", False),
    ],
)
def test_receipt_range_counts_unicode_characters(
    client: TestClient, status: str, length: object, accepted: bool
) -> None:
    start(client)
    saved = client.post("/turns", json=turn_body(text="茶🍵。", kind="observation"))
    assert saved.status_code == 201
    body = receipt_body(status=status, spoken_until=length, ignored="allowed by reference API")
    response = client.post("/turns/t-1/receipts", json=body)
    assert response.status_code == (201 if accepted else 422)
    expected = [{**receipt_body(status=status, spoken_until=length), "turn_id": "t-1"}]
    assert client.get("/turns/t-1/receipts").json() == (expected if accepted else [])
    assert client.get("/turns/t-1").json() == saved.json()


def test_ending_retry_preserves_report_and_updates_session_view(client: TestClient) -> None:
    start(client)
    assert client.get("/sessions/s-1").json()["ended_at"] is None
    assert client.get("/sessions/s-1/endings").json() == []
    body = {"ended_at": "2026-10-28T00:01:00Z", "reported_by": "Web"}
    before = datetime.now(UTC)
    first = client.post("/sessions/s-1/endings", json=body)
    assert first.status_code == 201
    saved = first.json()
    assert saved == {**body, "session_id": "s-1", "reported_at": saved["reported_at"]}
    assert before <= datetime.fromisoformat(saved["reported_at"]) <= datetime.now(UTC)
    retry = client.post("/sessions/s-1/endings", json=body)
    assert retry.status_code == 200
    assert retry.json() == saved
    conflict = client.post("/sessions/s-1/endings", json={**body, "reported_by": "Windows"})
    assert conflict.status_code == 409
    assert client.get("/sessions/s-1/endings").json() == [saved]
    assert client.get("/sessions/s-1").json() == {**session_body(), "ended_at": body["ended_at"]}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/sessions/missing", None),
        ("GET", "/sessions/missing/endings", None),
        ("POST", "/sessions/missing/endings", {"ended_at": STAMP, "reported_by": "Web"}),
        ("GET", "/turns/missing", None),
        ("GET", "/turns/missing/receipts", None),
        ("POST", "/turns/missing/receipts", receipt_body()),
        ("POST", "/turns", turn_body(session_id="missing")),
    ],
)
def test_missing_resources_return_evidence_not_found(
    client: TestClient, method: str, path: str, body: object
) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Evidence not found"}


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_turn_content_has_no_update_api(client: TestClient, method: str) -> None:
    start(client)
    saved = client.post("/turns", json=turn_body())
    assert saved.status_code == 201
    response = client.request(method, "/turns/t-1", json={"text": "changed"})
    assert response.status_code == 405
    assert client.get("/turns/t-1").json() == saved.json()


@pytest.mark.parametrize(
    ("path", "changes"),
    [
        ("/sessions", {"session_id": "bad/id"}),
        ("/sessions", {"started_at": "2026-10-28T00:00:00"}),
        ("/sessions", {"surface": " "}),
        ("/sessions", {"surface": 1}),
        ("/turns", {"turn_id": "bad/id"}),
        ("/turns", {"occurred_at": "2026-10-28T00:00:00"}),
        ("/turns", {"actor": " "}),
        ("/turns", {"text": 1}),
        ("/turns", {"language": " "}),
        ("/turns", {"sensitivity": " "}),
        ("/turns", {"classifier_version": " "}),
        ("/turns", {"register": {"tone": 1}}),
        ("/turns", {"audience": []}),
        ("/turns", {"recorded_at": STAMP}),
        ("/sessions/s-1/endings", {"ended_at": "2026-10-28T00:01:00"}),
        ("/sessions/s-1/endings", {"reported_by": " "}),
        ("/sessions/s-1/endings", {"reported_at": STAMP}),
        ("/turns/t-1/receipts", {"status": "unknown"}),
        ("/turns/t-1/receipts", {"reported_at": "2026-10-28T00:00:00"}),
        ("/turns/t-1/receipts", {"reported_by": " "}),
    ],
)
def test_request_validation_returns_only_public_error_fields(
    client: TestClient, path: str, changes: dict[str, object]
) -> None:
    bodies: dict[str, dict[str, object]] = {
        "/sessions": session_body(),
        "/turns": turn_body(),
        "/sessions/s-1/endings": {"ended_at": STAMP, "reported_by": "Web"},
        "/turns/t-1/receipts": receipt_body(),
    }
    response = client.post(path, json={**bodies[path], **changes})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors and all(set(error) == {"loc", "type", "msg"} for error in errors)
    assert any(
        error["loc"] == ["body", next(iter(changes))] or error["loc"][:2] == ["body", "register"]
        for error in errors
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/sessions", session_body()),
        ("GET", "/sessions/s-1", None),
        ("POST", "/sessions/s-1/endings", {"ended_at": STAMP, "reported_by": "Web"}),
        ("GET", "/sessions/s-1/endings", None),
        ("POST", "/turns", turn_body()),
        ("GET", "/turns/t-1", None),
        ("POST", "/turns/t-1/receipts", receipt_body()),
        ("GET", "/turns/t-1/receipts", None),
        ("GET", "/characters/char-a/turns", None),
    ],
)
def test_unavailable_service_returns_sanitized_503(method: str, path: str, body: object) -> None:
    http_module = importlib.import_module("digital_souls_memory_hindsight.evidence_http")
    ports = importlib.import_module("digital_souls_memory_hindsight.evidence.ports")

    def unavailable() -> object:
        raise ports.EvidenceUnavailable("synthetic internal connection detail")

    app = FastAPI()
    app.include_router(http_module.evidence_router(unavailable))
    with TestClient(app) as client:
        response = client.request(method, path, json=body)
    assert response.status_code == 503
    assert response.json() == {"detail": "Evidence unavailable"}
