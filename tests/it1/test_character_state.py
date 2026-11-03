"""State generations across HTTP mutations, PostgreSQL and the real worker."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.deletion.ports import DeletionStep
from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.deletion_http import AuthorCredentials, deletion_router
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from digital_souls_memory_hindsight.outbox import OutboxStore, RetainExecutor
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.worker import Worker
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import STAMP, receipt_body, session_body, turn_body

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.character_state.domain import StateInput
    from digital_souls_memory_hindsight.character_state.generator import CharacterStateGenerator

pytestmark = pytest.mark.it1
BASE = "/characters/char-b/sessions/s-1/state"
STATE = {"mood": "穏やか", "relationship_temperature": "温かい", "current_topic": "お茶"}


def test_state_cursor_collation_matches_python_order(database: str) -> None:
    identifiers = [f"t-{prefix}{i:02}" for prefix in ("A", "a", "_") for i in range(25)]
    expected = sorted(identifiers, reverse=True)
    with psycopg.connect(database, connect_timeout=5) as connection:
        first = connection.execute(
            "SELECT value FROM unnest(%s::text[]) AS value "
            'ORDER BY value COLLATE "C" DESC LIMIT 20',
            (identifiers,),
        ).fetchall()
        rest = connection.execute(
            "SELECT value FROM unnest(%s::text[]) AS value "
            'WHERE value COLLATE "C" < %s COLLATE "C" '
            'ORDER BY value COLLATE "C" DESC',
            (identifiers, first[-1][0]),
        ).fetchall()
    assert [row[0] for row in first + rest] == expected


class Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, tuple[StateInput, ...]]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.pause = False
        self.output: object = STATE
        self.error: Exception | None = None

    async def generate_state(
        self, character_id: str, session_id: str, surface: str, turns: tuple[StateInput, ...]
    ) -> object:
        self.calls.append((character_id, session_id, surface, turns))
        if self.error is not None:
            raise self.error
        if self.pause:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=10)
        return self.output


@pytest.fixture
def state_system(database: str) -> Iterator[tuple[TestClient, Worker, Gateway, Clock]]:
    service_module = importlib.import_module(
        "digital_souls_memory_hindsight.character_state.service"
    )
    generator_module = importlib.import_module(
        "digital_souls_memory_hindsight.character_state.generator"
    )
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_character_state")
    http = importlib.import_module("digital_souls_memory_hindsight.character_state_http")
    clock, gateway = Clock(), Gateway()
    evidence = EvidenceService(
        PostgresEvidenceStore(database), frozenset({"char-a", "char-b"}), clock=clock
    )
    deletion_store = PostgresDeletionStore(database)
    state_store = storage.PostgresCharacterStateStore(database)
    state_service = service_module.CharacterStateService(
        state_store, evidence, deletion_store, clock=clock
    )
    generator = generator_module.CharacterStateGenerator(
        state_store, evidence, deletion_store, gateway, clock=clock
    )
    worker = Worker(PostgresOutbox(database), None, evidence, clock=clock, state=generator)
    deletion = DeletionService(deletion_store, evidence, clock=clock)
    app = FastAPI()
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(http.character_state_router(lambda: state_service))
    app.include_router(
        deletion_router(lambda: deletion, AuthorCredentials("synthetic-author", "fixture"))
    )
    with TestClient(app) as client:
        response = client.post("/sessions", json=session_body())
        assert response.status_code == 201, response.text
        yield client, worker, gateway, clock


def post_turn(client: TestClient, identifier: str = "t-1", **changes: object) -> None:
    response = client.post(
        "/turns",
        json={
            **turn_body(
                turn_id=identifier, actor="user-a", perceived_by=["char-b"], text="架空のお茶🍵"
            ),
            **changes,
        },
    )
    assert response.status_code == 201, response.text


def get_state(client: TestClient, base: str = BASE, surface: str = "Web") -> object:
    response = client.get(base, params={"surface": surface})
    assert response.status_code == 200, response.text
    return response.json()


def row(database: str) -> tuple[object, ...]:
    with psycopg.connect(database, connect_timeout=5) as connection:
        value = connection.execute(
            "SELECT generation, state_generation, mood, relationship_temperature, current_topic, "
            "generated_at FROM character_states WHERE character_id='char-b' "
            "AND session_id='s-1' AND surface='Web'"
        ).fetchone()
    assert value is not None
    return value


def test_turn_to_worker_to_get_and_no_regeneration(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, clock = state_system
    post_turn(client)
    assert gateway.calls == []
    assert get_state(client) == {"state": None}
    clock.now += timedelta(seconds=60)
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    assert row(database)[:2] == (1, 1)
    assert row(database)[5] == clock.now
    assert [
        (c, s, surface, [(t.turn_id, t.text) for t in turns])
        for c, s, surface, turns in gateway.calls
    ] == [("char-b", "s-1", "Web", [("t-1", "架空のお茶🍵")])]
    for _ in range(3):
        asyncio.run(worker.run_once())
        assert get_state(client) == {"state": STATE}
    assert len(gateway.calls) == 1
    post_turn(client)
    assert row(database)[:2] == (1, 1)
    assert get_state(client, BASE.replace("char-b", "char-a")) == {"state": None}
    assert get_state(client, surface="App") == {"state": None}
    assert client.post("/sessions", json=session_body(session_id="s-2")).status_code == 201
    assert get_state(client, BASE.replace("s-1", "s-2")) == {"state": None}


def test_invalid_generation_does_not_restore_previous_state_or_regenerate_until_next_input(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, _ = state_system
    post_turn(client)
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    post_turn(client, "t-2")
    gateway.output = {"mood": "incomplete"}
    for _ in range(3):
        asyncio.run(worker.run_once())
        assert get_state(client) == {"state": None}
    assert len(gateway.calls) == 2
    assert row(database)[:2] == (2, 1)
    post_turn(client, "t-3")
    gateway.output = STATE
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    assert row(database)[:2] == (3, 3)


def test_transient_failure_waits_for_persisted_deadlines_and_stops_at_limit(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, clock = state_system
    post_turn(client)
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    post_turn(client, "t-2")
    gateway.error = RuntimeError("synthetic private content")
    for attempt, delay in ((1, 1), (2, 2), (3, 60)):
        asyncio.run(worker.run_once())
        assert len(gateway.calls) == attempt + 1
        assert get_state(client) == {"state": None}
        asyncio.run(worker.run_once())
        assert len(gateway.calls) == attempt + 1
        clock.now += timedelta(seconds=delay)
    asyncio.run(worker.run_once())
    assert len(gateway.calls) == 4
    assert row(database)[:2] == (2, 1)
    post_turn(client, "t-3")
    gateway.error = None
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    assert row(database)[:2] == (3, 3)


def test_tombstoned_turn_is_absent_from_generation_and_cannot_be_resent(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, clock = state_system
    post_turn(client)
    post_turn(client, "kept")
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    store = PostgresDeletionStore(database)
    deletion = DeletionService(
        store,
        EvidenceService(PostgresEvidenceStore(database), frozenset({"char-a", "char-b"})),
        clock=clock,
    )
    request = deletion.accept("synthetic-delete", "synthetic-author", ("t-1",), None)
    assert get_state(client) == {"state": None}
    item = store.claim(clock.now, timedelta(seconds=60))
    assert item is not None
    assert store.delete_turns(item, request, clock.now)["ledger_done"] is True
    assert client.get("/turns/t-1").status_code == 404
    resend = client.post(
        "/turns", json=turn_body(actor="user-a", perceived_by=["char-b"], text="架空のお茶🍵")
    )
    assert resend.status_code == 409
    asyncio.run(worker.run_once())
    assert [(t.turn_id, t.text) for t in gateway.calls[-1][3]] == [("kept", "架空のお茶🍵")]
    assert get_state(client) == {"state": None}


def test_state_enqueue_failure_rolls_back_turn_and_projection_outbox(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, _, _, _ = state_system
    with psycopg.connect(database, connect_timeout=5) as connection:
        connection.execute("""
            CREATE FUNCTION reject_state_change() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic state failure'; END; $$;
            CREATE TRIGGER reject_state BEFORE INSERT OR UPDATE ON character_states
                FOR EACH ROW EXECUTE FUNCTION reject_state_change();
        """)
    response = client.post("/turns", json=turn_body(actor="user-a", perceived_by=["char-b"]))
    assert response.status_code == 503, response.text
    assert client.get("/turns/t-1").status_code == 404
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM outbox").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM character_states").fetchone() == (0,)
        connection.execute("DROP TRIGGER reject_state ON character_states")
    post_turn(client)
    assert row(database)[:2] == (1, None)


def test_receipt_correction_invalidates_then_uses_only_delivered_text(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, _ = state_system
    response = client.post(
        "/turns",
        json=turn_body(actor="char-a", perceived_by=["char-a", "char-b"], text="茶🍵。秘密"),
    )
    assert response.status_code == 201, response.text
    for status, limit in (("full", 5), ("partial", 2), ("none", 0)):
        response = client.post(
            "/turns/t-1/receipts", json=receipt_body(status=status, spoken_until=limit)
        )
        assert response.status_code == 201, response.text
        assert get_state(client) == {"state": None}
        for _ in range(2):
            asyncio.run(worker.run_once())
        if limit:
            assert get_state(client) == {"state": STATE}
            call = [call for call in gateway.calls if call[0] == "char-b"][-1]
            assert [(t.turn_id, t.text) for t in call[3]] == [("t-1", "茶🍵。秘密"[:limit])]
        else:
            assert get_state(client) == {"state": None}
    assert row(database)[0] == 4


@pytest.mark.parametrize("status", ["accepted", "processing", "failed"])
def test_deletion_acceptance_immediately_hides_state_and_excludes_target_input(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str, status: str
) -> None:
    client, worker, gateway, clock = state_system
    post_turn(client)
    post_turn(client, "kept")
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    response = client.post(
        "/deletion-requests",
        headers={"Authorization": "Bearer fixture"},
        json={"request_id": "synthetic-delete", "target_turn_ids": ["t-1"]},
    )
    assert response.status_code == 202, response.text
    assert get_state(client) == {"state": None}
    assert row(database)[0] == 3
    deletion = DeletionService(
        PostgresDeletionStore(database),
        EvidenceService(PostgresEvidenceStore(database), frozenset({"char-a", "char-b"})),
        clock=clock,
    )
    if status != "accepted":
        deletion.transition("synthetic-delete", "processing")
    if status == "failed":
        deletion.transition("synthetic-delete", "failed", error="synthetic_failure")
    asyncio.run(worker.run_once())
    assert [(t.turn_id, t.text) for t in gateway.calls[-1][3]] == [("kept", "架空のお茶🍵")]
    assert get_state(client) == {"state": None}


def test_generation_in_flight_is_rejected_after_new_turn_then_next_generation_succeeds(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, _ = state_system
    post_turn(client)

    async def exercise() -> None:
        gateway.pause = True
        pending = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(gateway.entered.wait(), 5)
            post_turn(client, "t-2")
        finally:
            gateway.release.set()
            await asyncio.wait_for(pending, 10)
        assert get_state(client) == {"state": None}
        assert row(database)[:2] == (2, None)
        gateway.pause = False
        await worker.run_once()
        assert get_state(client) == {"state": STATE}
        assert row(database)[:2] == (2, 2)

    asyncio.run(exercise())


def test_ending_clears_state_and_preserves_generation_after_later_turn(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, _ = state_system
    for identifier in ("t-1", "t-2", "t-3"):
        post_turn(client, identifier)

    async def exercise() -> None:
        gateway.pause = True
        pending = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(gateway.entered.wait(), 5)
            response = client.post(
                "/sessions/s-1/endings", json={"ended_at": STAMP, "reported_by": "Web"}
            )
            assert response.status_code == 201, response.text
            post_turn(client, "t-4")
        finally:
            gateway.release.set()
            await asyncio.wait_for(pending, 10)
        assert row(database) == (5, None, None, None, None, None)
        assert get_state(client) == {"state": None}
        await worker.run_once()
        assert len(gateway.calls) == 1
        assert get_state(client) == {"state": None}

    asyncio.run(exercise())


def test_generated_state_is_erased_immediately_at_session_end(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, _ = state_system
    post_turn(client)
    asyncio.run(worker.run_once())
    assert get_state(client) == {"state": STATE}
    response = client.post("/sessions/s-1/endings", json={"ended_at": STAMP, "reported_by": "Web"})
    assert response.status_code == 201, response.text
    assert get_state(client) == {"state": None}
    assert row(database) == (2, None, None, None, None, None)
    response = client.post("/sessions/s-1/endings", json={"ended_at": STAMP, "reported_by": "Web"})
    assert response.status_code == 200
    assert row(database)[0] == 2
    asyncio.run(worker.run_once())
    assert len(gateway.calls) == 1


def test_assumed_confirmation_advances_generation_and_exposes_delivered_input(
    state_system: tuple[TestClient, Worker, Gateway, Clock], database: str
) -> None:
    client, worker, gateway, clock = state_system
    response = client.post(
        "/turns", json=turn_body(perceived_by=["char-a", "char-b"], text="茶🍵。秘密")
    )
    assert response.status_code == 201, response.text
    for _ in range(2):
        asyncio.run(worker.run_once())
    assert get_state(client) == {"state": None}
    assert all(call[0] != "char-b" for call in gateway.calls)
    assert row(database)[0] == 1
    clock.now += timedelta(seconds=600)
    for _ in range(2):
        asyncio.run(worker.run_once())
    assert row(database)[:2] == (2, 2)
    assert get_state(client) == {"state": STATE}
    call = [call for call in gateway.calls if call[0] == "char-b"][-1]
    assert [(t.turn_id, t.text) for t in call[3]] == [("t-1", "茶🍵。秘密")]


def test_gateway_input_excludes_other_sessions_surfaces_and_unperceived_turns(
    state_system: tuple[TestClient, Worker, Gateway, Clock],
) -> None:
    client, worker, gateway, _ = state_system
    assert client.post("/sessions", json=session_body(session_id="s-2")).status_code == 201
    post_turn(client, "other-session", session_id="s-2")
    post_turn(client, "other-surface", surface="App")
    post_turn(client, "hidden", perceived_by=["char-a"])
    post_turn(client, "visible")
    for _ in range(4):
        asyncio.run(worker.run_once())
    calls = [
        turns
        for c, s, surface, turns in gateway.calls
        if (c, s, surface) == ("char-b", "s-1", "Web")
    ]
    assert len(calls) == 1
    assert [(t.turn_id, t.text) for t in calls[0]] == [("visible", "架空のお茶🍵")]


@pytest.mark.parametrize("pending", [0, 25])
def test_paged_input_preserves_order_across_pending_turns_and_tied_timestamps(
    state_system: tuple[TestClient, Worker, Gateway, Clock], pending: int
) -> None:
    client, worker, gateway, _ = state_system
    identifiers = [f"t-{prefix}{i:02}" for prefix in ("A", "a", "_") for i in range(17)][:50]
    newest = sorted(identifiers, reverse=True)
    delivered = newest[pending:]
    for identifier in identifiers:
        post_turn(client, identifier, actor="char-a", perceived_by=["char-a", "char-b"])
        if identifier in delivered:
            response = client.post(
                f"/turns/{identifier}/receipts",
                json=receipt_body(status="full", spoken_until=len("架空のお茶🍵")),
            )
            assert response.status_code == 201, response.text
    for _ in range(2):
        asyncio.run(worker.run_once())
    calls = [turns for c, _, _, turns in gateway.calls if c == "char-b"]
    assert len(calls) == 1
    assert [(turn.turn_id, turn.text) for turn in calls[0]] == [
        (identifier, "架空のお茶🍵") for identifier in reversed(delivered[:20])
    ]
    assert get_state(client) == {"state": STATE}


@pytest.mark.parametrize("configured", [False, True])
def test_production_lifespan_injects_gateway_and_preserves_null_without_port(
    database: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured: bool
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a", "char-b"]')
    monkeypatch.setenv("CHARACTER_STATE_INPUT_MAX_CHARS", "2")
    monkeypatch.setenv("CHARACTER_STATE_TTL_SECONDS", "1")
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    clock = Clock()
    monkeypatch.setattr(
        module, "CharacterStateService", partial(module.CharacterStateService, clock=clock)
    )
    gateway = Gateway()
    workers: list[Worker] = []

    def worker_factory(
        store: OutboxStore,
        executor: RetainExecutor | None,
        evidence: EvidenceService,
        *,
        deletion: DeletionStep | None = None,
        state: CharacterStateGenerator | None = None,
    ) -> Worker:
        # Preserve the real worker while the test controls its scheduling.
        worker = Worker(
            store,
            executor,
            evidence,
            deletion=deletion,
            state=state,
        )
        workers.append(worker)
        return worker

    monkeypatch.setattr(Worker, "run", AsyncMock())
    monkeypatch.setattr(module, "Worker", worker_factory)
    app = module.create_app(context_gateway=gateway) if configured else module.create_app()
    with TestClient(app) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        post_turn(client, "old", text="長すぎる架空入力")
        post_turn(client, "new", text="茶🍵", occurred_at="2026-10-28T00:00:01Z")
        assert get_state(client) == {"state": None}
        assert gateway.calls == []
        asyncio.run(workers[0].run_once())
        clock.now = datetime.now(UTC)
        assert get_state(client) == {"state": STATE if configured else None}
        if configured:
            assert [(t.turn_id, t.text) for t in gateway.calls[0][3]] == [("new", "茶🍵")]
            clock.now += timedelta(seconds=2)
            assert get_state(client) == {"state": None}
        else:
            assert gateway.calls == []
