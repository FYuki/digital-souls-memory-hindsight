"""Voice exemplar persistence, revocation and deletion on disposable PostgreSQL."""

import asyncio
import importlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import cast
from unittest.mock import patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.deletion_http import AuthorCredentials, deletion_router
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_identity import PostgresIdentityStore
from tests.consistency_doubles import Gateway, Reviewer
from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import Projection, hindsight_client
from tests.it1.test_deletion_execution import finish_deletion
from tests.ut.test_voice_http import SEED, TEXT, span, turn_voice, voice_body

pytestmark = pytest.mark.it1
projection = projection_tests.projection


def voices(p: Projection) -> TestClient:
    module = importlib.import_module("digital_souls_memory_hindsight.voice.service")
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_voice")
    http = importlib.import_module("digital_souls_memory_hindsight.voice_http")
    deletion_store = PostgresDeletionStore(p.database)
    deletion = DeletionService(deletion_store, p.service, clock=p.clock)
    instance = module.VoiceService(
        storage.PostgresVoiceStore(p.database),
        p.service,
        deletion_store,
        PostgresIdentityStore(p.database),
        Gateway(),
        Reviewer(),
        clock=p.clock,
    )
    app = FastAPI()
    app.include_router(http.voice_router(lambda: instance))
    # Synthetic credentials are local to the disposable test app.
    app.include_router(
        deletion_router(lambda: deletion, AuthorCredentials("synthetic-author", "fixture"))
    )
    return TestClient(app)


def base(p: Projection) -> str:
    return f"/characters/{p.characters[0]}/voice-exemplars"


def seed_qa(p: Projection) -> dict[str, object]:
    p.post_turn("q", text=TEXT)
    p.receipt("q", len(TEXT), len(TEXT))
    p.post_turn("a", text=TEXT, reply_to_turn_id="q")
    p.receipt("a", len(TEXT), len(TEXT))
    return turn_voice(question="😀b", question_span=span("q", 1, 3))


@pytest.mark.parametrize("qa", [False, True])
def test_real_database_roundtrip_preserves_seed_verbatim(projection: Projection, qa: bool) -> None:
    p = projection
    body = voice_body(**({"question": SEED} if qa else {}))
    with voices(p) as client:
        created = client.post(base(p), json=body)
        assert created.status_code == 201, created.text
    # Recreate the service to ensure get/list restore the persisted record.
    with voices(p) as client:
        saved = {k: v for k, v in created.json().items() if k != "consistency_review"}
        assert client.get(base(p) + "/voice-1").json() == saved
        assert client.get(base(p)).json() == [saved]
        assert created.json()["answer"] == SEED
        assert created.json().get("question") == (SEED if qa else None)


@pytest.mark.parametrize("side, limit", [("a", 3), ("q", 2)])
def test_receipt_correction_hides_whole_saved_qa(
    projection: Projection, side: str, limit: int
) -> None:
    p = projection
    body = seed_qa(p)
    with voices(p) as client:
        created = client.post(base(p), json=body)
        assert created.status_code == 201, created.text
        saved = {k: v for k, v in created.json().items() if k != "consistency_review"}
        assert client.get(base(p) + "/voice-1").json() == saved
        assert client.get(base(p)).json() == [saved]
        p.receipt(side, limit, len(TEXT))
        assert client.get(base(p) + "/voice-1").status_code == 404
        assert client.get(base(p)).json() == []
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute("SELECT count(*) FROM voice_exemplars").fetchone() == (1,)


@pytest.mark.parametrize("status", ["accepted", "processing", "failed"])
def test_unfinished_deletion_hides_qa_and_rejects_registration(
    projection: Projection, status: str
) -> None:
    p = projection
    body = seed_qa(p)
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with voices(p) as client:
        created = client.post(base(p), json=body)
        assert created.status_code == 201, created.text
        assert client.get(base(p) + "/voice-1").status_code == 200
        service.accept("delete-q", "synthetic-author", ("q",), None)
        if status != "accepted":
            service.transition("delete-q", "processing")
        if status == "failed":
            service.transition("delete-q", "failed", error="synthetic_failure")
        assert client.get(base(p) + "/voice-1").status_code == 404
        assert client.get(base(p)).json() == []
        assert client.post(base(p), json={**body, "exemplar_id": "new"}).status_code == 422
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute("SELECT count(*) FROM voice_exemplars").fetchone() == (1,)


@pytest.mark.parametrize("side", ["q", "a"])
def test_author_deletion_removes_entire_qa_and_records_exemplar_id(
    projection: Projection, side: str
) -> None:
    p = projection
    body = seed_qa(p)
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with voices(p) as client:
        assert client.post(base(p), json=body).status_code == 201
        assert client.post(base(p), json=voice_body(exemplar_id="kept")).status_code == 201

        async def exercise() -> None:
            async with hindsight_client() as hindsight:
                await p.drain(hindsight)
                accepted = client.post(
                    "/deletion-requests",
                    headers={"Authorization": "Bearer fixture"},
                    json={"request_id": "delete-qa", "target_turn_ids": [side]},
                )
                assert accepted.status_code == 202, accepted.text
                await finish_deletion(p, hindsight, service, store, "delete-qa")

        asyncio.run(exercise())
        result = client.get(
            "/deletion-requests/delete-qa", headers={"Authorization": "Bearer fixture"}
        )
        assert result.status_code == 200
        assert result.json()["cascade_result"]["deleted_voice_exemplars"] == [
            {"character_id": p.characters[0], "exemplar_id": "voice-1"}
        ]
        assert client.get(base(p) + "/voice-1").status_code == 404
        assert [v["exemplar_id"] for v in client.get(base(p)).json()] == ["kept"]
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT exemplar_id FROM voice_exemplars ORDER BY exemplar_id"
            ).fetchall() == [("kept",)]


def test_registration_rechecks_source_after_concurrent_deletion(projection: Projection) -> None:
    p = projection
    body = seed_qa(p)
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_voice")
    reached_store = Event()
    release = Event()
    original_add = storage.PostgresVoiceStore.add

    def delayed_add(self: object, value: object, review: object) -> bool:
        reached_store.set()
        assert release.wait(15), "Deletion did not release registration"
        return cast(bool, original_add(self, value, review))

    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with voices(p) as client, ThreadPoolExecutor(max_workers=1) as executor:
        with patch.object(storage.PostgresVoiceStore, "add", delayed_add):
            pending = executor.submit(client.post, base(p), json=body)
            try:
                assert reached_store.wait(10), "Registration did not reach persistence"
                request = service.accept("race", "synthetic-author", ("q",), None)
                item = store.claim(p.clock.now, timedelta(seconds=60))
                assert item is not None
                progress = store.delete_turns(item, request, p.clock.now)
                assert progress["ledger_done"] is True
            finally:
                release.set()
            response = pending.result(timeout=15)
        assert response.status_code == 422, response.text
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute("SELECT count(*) FROM voice_exemplars").fetchone() == (0,)
            assert connection.execute(
                "SELECT turn_id FROM turn_tombstones WHERE turn_id='q'"
            ).fetchone() == ("q",)


def test_deletion_waits_for_inflight_registration_then_cascades(projection: Projection) -> None:
    p = projection
    body = seed_qa(p)
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    # Block INSERT after it has acquired its source locks, without replacing store logic.
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        connection.execute("""
            CREATE FUNCTION pause_voice_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_advisory_xact_lock(46); RETURN NEW; END; $$;
            CREATE TRIGGER pause_voice BEFORE INSERT ON voice_exemplars
                FOR EACH ROW EXECUTE FUNCTION pause_voice_insert();
        """)
    with (
        voices(p) as client,
        psycopg.connect(p.database, connect_timeout=5, autocommit=True) as gate,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        gate.execute("SELECT pg_advisory_lock(46)")
        inserted = executor.submit(client.post, base(p), json=body)
        try:
            deadline = time.monotonic() + 10
            while True:
                row = gate.execute(
                    "SELECT pid FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                    "AND objid=46 AND database=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database())"
                ).fetchone()
                if row is not None:
                    registration_pid = row[0]
                    break
                assert not inserted.done(), "Registration terminated before the INSERT gate"
                assert time.monotonic() < deadline, "Registration did not reach INSERT"
                time.sleep(0.01)
            accepted = executor.submit(
                service.accept, "race-insert", "synthetic-author", ("q",), None
            )
            while True:
                blocked = gate.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                    (registration_pid,),
                ).fetchone()
                if blocked is not None:
                    break
                assert not accepted.done(), (
                    "Deletion acceptance bypassed the in-flight source locks"
                )
                assert time.monotonic() < deadline, "Deletion did not wait for registration"
                time.sleep(0.01)
        finally:
            gate.execute("SELECT pg_advisory_unlock(46)")
        response = inserted.result(timeout=15)
        assert response.status_code == 201, response.text
        request = accepted.result(timeout=15)
        item = store.claim(p.clock.now, timedelta(seconds=60))
        assert item is not None
        progress = store.delete_turns(item, request, p.clock.now)
        assert progress["deleted_voice_exemplars"] == [
            {"character_id": p.characters[0], "exemplar_id": "voice-1"}
        ]
        assert gate.execute("SELECT count(*) FROM voice_exemplars").fetchone() == (0,)
        assert gate.execute("SELECT turn_id FROM turn_tombstones WHERE turn_id='q'").fetchone() == (
            "q",
        )
