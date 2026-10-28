"""Persisted delivery decisions through HTTP and independent ledger connections."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, current_thread
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt, Session, Turn, TurnInput
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from tests.it1.test_evidence_ledger import create_session, turn_body

pytestmark = pytest.mark.it1
CHARACTERS = frozenset({"char-a", "char-b"})
NOW = datetime(2026, 10, 28, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=os.environ["SERVICE_URL"], timeout=10, trust_env=False) as value:
        yield value


def ledger(clock: Clock | None = None) -> EvidenceService:
    store = PostgresEvidenceStore(os.environ["DATABASE_URL"])
    return (
        EvidenceService(store, CHARACTERS)
        if clock is None
        else EvidenceService(store, CHARACTERS, clock=clock)
    )


def delivery_rows(turn_id: str) -> list[tuple[object, ...]]:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        table = delivery_table(connection)
        rows = connection.execute(
            sql.SQL("SELECT * FROM {} WHERE turn_id = %s").format(sql.Identifier(table)),
            (turn_id,),
        ).fetchall()
        return sorted(rows, key=repr)


def delivery_table(connection: psycopg.Connection[tuple[object, ...]]) -> str:
    rows = connection.execute(
        "SELECT table_name, array_agg(column_name::text) FROM information_schema.columns "
        "WHERE table_schema = 'public' GROUP BY table_name"
    ).fetchall()
    matches = [
        str(name)
        for name, columns in rows
        if isinstance(columns, list)
        and {"turn_id", "spoken_until"} <= set(columns)
        and "reported_by" not in columns
    ]
    assert len(matches) == 1, matches
    return matches[0]


@pytest.mark.parametrize(
    ("reports_delivery", "actor", "kind"),
    [
        (False, "char-a", "utterance"),
        (True, "user-a", "utterance"),
        (True, "char-a", "observation"),
        (True, "char-a", "thought"),
    ],
)
def test_http_registration_restores_initial_delivery_scope(
    client: httpx.Client, reports_delivery: bool, actor: str, kind: str
) -> None:
    session_id = create_session(client, reports_delivery)
    body = turn_body(
        session_id,
        actor=actor,
        kind=kind,
        perceived_by=["char-a"] if kind == "thought" else ["char-a", "char-b"],
    )
    response = client.post("/turns", json=body)
    assert response.status_code == 201, response.text
    turn_id = str(body["turn_id"])
    reader = ledger()
    scope = reader.get_delivery_scope(turn_id)
    if kind == "thought":
        assert scope is None
        assert delivery_rows(turn_id) == []
        assert reader.get_projection_text(turn_id, "char-a") == body["text"]
        assert reader.get_projection_text(turn_id, "char-b") is None
    else:
        assert scope is not None
        assert scope.spoken_until == len(str(body["text"]))
        assert reader.get_projection_text(turn_id, "char-b") == body["text"]
        assert len(delivery_rows(turn_id)) == 1
    assert client.get(f"/turns/{turn_id}").json() == response.json()


@pytest.mark.parametrize(("status", "limit"), [("partial", 3), ("none", 0), ("full", 7)])
def test_http_receipt_updates_same_query_and_commits_range_without_changing_turn(
    client: httpx.Client, status: str, limit: int
) -> None:
    session_id = create_session(client, True)
    text = " 茶🍵。秘密\n"
    body = turn_body(session_id, text=text, perceived_by=["char-a", "char-b"])
    response = client.post("/turns", json=body)
    assert response.status_code == 201, response.text
    turn_id = str(body["turn_id"])
    reader = ledger(
        Clock(datetime.fromisoformat(response.json()["recorded_at"]) + timedelta(seconds=600))
    )
    pending = reader.get_delivery_scope(turn_id)
    assert pending is not None and pending.spoken_until is None
    assert reader.get_projection_text(turn_id, "char-b") is None
    assert reader.get_projection_text(turn_id, "char-a") == text
    before = delivery_rows(turn_id)
    assert len(before) == 1
    receipt = client.post(
        f"/turns/{turn_id}/receipts",
        json={
            "status": status,
            "spoken_until": limit,
            "reported_by": "Web",
            "reported_at": NOW.isoformat(),
        },
    )
    assert receipt.status_code == 201, receipt.text
    scope = reader.get_delivery_scope(turn_id)
    assert scope is not None
    assert scope.status != pending.status
    assert scope.spoken_until == limit
    assert reader.get_projection_text(turn_id, "char-b") == text[:limit]
    assert reader.get_projection_text(turn_id, "char-a") == text
    assert ledger().get_delivery_scope(turn_id) == scope
    assert turn_id not in reader.confirm_expired_deliveries()
    assert reader.get_delivery_scope(turn_id) == scope
    after = delivery_rows(turn_id)
    assert len(after) == 2
    assert all(row in after for row in before)
    assert client.post("/turns", json=body).json() == response.json()
    assert len(delivery_rows(turn_id)) == 2
    assert client.get(f"/turns/{turn_id}").json() == response.json()
    assert client.get("/characters/char-b/turns").json().count(response.json()) == 1


def test_expiry_then_late_receipt_preserves_decisions_across_connections(
    client: httpx.Client,
) -> None:
    body = turn_body(create_session(client, True), perceived_by=["char-a", "char-b"])
    response = client.post("/turns", json=body)
    assert response.status_code == 201, response.text
    turn_id = str(body["turn_id"])
    recorded_at = datetime.fromisoformat(response.json()["recorded_at"])
    clock = Clock(recorded_at + timedelta(seconds=600) - timedelta(microseconds=1))
    service = ledger(clock)
    pending = service.get_delivery_scope(turn_id)
    assert service.get_projection_text(turn_id, "char-b") is None
    assert turn_id not in service.confirm_expired_deliveries()
    clock.now += timedelta(microseconds=1)
    assert turn_id in service.confirm_expired_deliveries()
    assumed = service.get_delivery_scope(turn_id)
    assert assumed is not None and pending is not None
    assert assumed.status != pending.status
    assert ledger().get_projection_text(turn_id, "char-b") == body["text"]
    assert service.list_receipts(turn_id) == ()
    history = delivery_rows(turn_id)
    assert len(history) == 2
    assert turn_id not in service.confirm_expired_deliveries()
    assert len(delivery_rows(turn_id)) == 2
    service.add_receipt(DeliveryReceipt(turn_id, "partial", 3, "Web", NOW))
    confirmed = service.get_delivery_scope(turn_id)
    assert confirmed is not None
    assert confirmed.status != assumed.status
    assert service.get_projection_text(turn_id, "char-b") == str(body["text"])[:3]
    after = delivery_rows(turn_id)
    assert len(after) == 3
    assert all(row in after for row in history)
    assert client.get(f"/turns/{turn_id}").json() == response.json()


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_database_rejects_modification_of_delivery_history(
    client: httpx.Client, operation: str
) -> None:
    body = turn_body(create_session(client, False))
    assert client.post("/turns", json=body).status_code == 201
    turn_id = str(body["turn_id"])
    before = delivery_rows(turn_id)
    assert len(before) == 1
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        table = sql.Identifier(delivery_table(connection))
        queries = {
            "UPDATE": sql.SQL("UPDATE {} SET turn_id = turn_id WHERE turn_id = %s").format(table),
            "DELETE": sql.SQL("DELETE FROM {} WHERE turn_id = %s").format(table),
            "TRUNCATE": sql.SQL("TRUNCATE {}").format(table),
        }
        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(queries[operation], () if operation == "TRUNCATE" else (turn_id,))
    assert delivery_rows(turn_id) == before


@pytest.mark.parametrize("operation", ["turn", "receipt", "expiry"])
def test_delivery_failure_rolls_back_real_ledger_and_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch, operation: str, database: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", database)
    clock = Clock(NOW)
    service = ledger(clock)
    session_id = "synthetic-" + uuid4().hex
    turn_id = "synthetic-" + uuid4().hex
    service.add_session(Session(session_id, NOW, None, "Web", True))
    draft = TurnInput(
        turn_id,
        session_id,
        "Web",
        "char-a",
        "utterance",
        "茶🍵。",
        ("char-a", "char-b"),
        (),
        "broadcast",
        None,
        "ja",
        {},
        "public",
        "fixture-1",
        NOW,
    )
    if operation != "turn":
        service.add_turn(draft)
    before = delivery_rows(turn_id)
    clock.now += timedelta(seconds=600)

    def perform() -> None:
        if operation == "turn":
            service.add_turn(draft)
        elif operation == "receipt":
            service.add_receipt(DeliveryReceipt(turn_id, "partial", 2, "Web", NOW))
        else:
            service.confirm_expired_deliveries()

    def fail_write(self: PostgresEvidenceStore, value: object) -> None:
        raise EvidenceUnavailable("synthetic delivery write failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresEvidenceStore, "add_delivery_scope", fail_write)
        with pytest.raises(EvidenceUnavailable):
            perform()
    assert delivery_rows(turn_id) == before
    store = PostgresEvidenceStore(os.environ["DATABASE_URL"])
    assert store.list_receipts(turn_id) == ()
    if operation == "turn":
        assert store.get_turn(turn_id) is None
    else:
        assert ledger().get_projection_text(turn_id, "char-b") is None
    perform()
    assert len(delivery_rows(turn_id)) == len(before) + 1
    expected = None if operation == "turn" else "茶🍵" if operation == "receipt" else "茶🍵。"
    assert ledger().get_projection_text(turn_id, "char-b") == expected


def test_receipt_and_expiry_on_separate_connections_keep_receipt_as_final_range(
    client: httpx.Client,
) -> None:
    body = turn_body(create_session(client, True), perceived_by=["char-a", "char-b"])
    response = client.post("/turns", json=body)
    assert response.status_code == 201
    turn_id = str(body["turn_id"])
    deadline = datetime.fromisoformat(response.json()["recorded_at"]) + timedelta(seconds=600)
    barrier = Barrier(2)

    def receive() -> None:
        barrier.wait(timeout=10)
        ledger(Clock(deadline)).add_receipt(DeliveryReceipt(turn_id, "partial", 3, "Web", NOW))

    def expire() -> None:
        barrier.wait(timeout=10)
        ledger(Clock(deadline)).confirm_expired_deliveries()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(receive), executor.submit(expire)]
        for future in futures:
            future.result(timeout=30)
    reader = ledger(Clock(deadline))
    scope = reader.get_delivery_scope(turn_id)
    assert scope is not None and scope.spoken_until == 3
    assert reader.get_projection_text(turn_id, "char-b") == str(body["text"])[:3]
    history = delivery_rows(turn_id)
    assert len(history) in {2, 3}
    assert turn_id not in reader.confirm_expired_deliveries()
    assert delivery_rows(turn_id) == history


def test_concurrent_identical_registration_creates_one_initial_decision(
    monkeypatch: pytest.MonkeyPatch,
    database: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database)
    clock = Clock(NOW)
    service = ledger(clock)
    session_id = "synthetic-" + uuid4().hex
    turn_id = "synthetic-" + uuid4().hex
    service.add_session(Session(session_id, NOW, None, "Web", True))
    draft = TurnInput(
        turn_id,
        session_id,
        "Web",
        "char-a",
        "utterance",
        "茶🍵。",
        ("char-a", "char-b"),
        (),
        "broadcast",
        None,
        "ja",
        {},
        "public",
        "fixture-1",
        NOW,
    )
    barrier = Barrier(2)

    def register(offset: int) -> datetime:
        caller = ledger(Clock(NOW + timedelta(seconds=offset)))
        barrier.wait(timeout=10)
        return caller.add_turn(draft).recorded_at

    with ThreadPoolExecutor(max_workers=2) as executor:
        times = list(executor.map(register, [0, 1]))
    assert times[0] == times[1]
    assert len(delivery_rows(turn_id)) == 1
    clock.now = times[0] + timedelta(seconds=600) - timedelta(microseconds=1)
    assert turn_id not in service.confirm_expired_deliveries()
    clock.now += timedelta(microseconds=1)
    assert turn_id in service.confirm_expired_deliveries()


@pytest.mark.parametrize("first_operation", ["receipt", "expiry"])
def test_receipt_and_expiry_follow_each_turn_lock_order(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch, first_operation: str
) -> None:
    body = turn_body(create_session(client, True), perceived_by=["char-a", "char-b"])
    response = client.post("/turns", json=body)
    assert response.status_code == 201, response.text
    turn_id = str(body["turn_id"])
    deadline = datetime.fromisoformat(response.json()["recorded_at"]) + timedelta(seconds=600)
    locked, attempted, release = Event(), Event(), Event()
    original = PostgresEvidenceStore.get_turn_for_update

    def controlled_lock(self: PostgresEvidenceStore, identifier: str) -> Turn | None:
        if identifier != turn_id:
            return original(self, identifier)
        if current_thread().name.startswith("first-delivery"):
            turn = original(self, identifier)
            locked.set()
            assert release.wait(timeout=10)
            return turn
        attempted.set()
        return original(self, identifier)

    monkeypatch.setattr(PostgresEvidenceStore, "get_turn_for_update", controlled_lock)

    def perform(operation: str) -> None:
        service = ledger(Clock(deadline))
        if operation == "receipt":
            service.add_receipt(DeliveryReceipt(turn_id, "partial", 3, "Web", NOW))
        else:
            service.confirm_expired_deliveries()

    second_operation = "expiry" if first_operation == "receipt" else "receipt"
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-delivery") as first,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="second-delivery") as second,
    ):
        first_result = first.submit(perform, first_operation)
        try:
            assert locked.wait(timeout=10)
            second_result = second.submit(perform, second_operation)
            assert attempted.wait(timeout=10)
        finally:
            release.set()
        first_result.result(timeout=30)
        second_result.result(timeout=30)
    reader = ledger(Clock(deadline))
    assert reader.get_projection_text(turn_id, "char-b") == str(body["text"])[:3]
    assert len(delivery_rows(turn_id)) == (2 if first_operation == "receipt" else 3)
    assert turn_id not in reader.confirm_expired_deliveries()
    assert client.get(f"/turns/{turn_id}").json() == response.json()
