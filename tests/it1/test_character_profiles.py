"""Persisted profiles track delivery correction and author deletion."""

import asyncio
import importlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from unittest.mock import patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.deletion_http import AuthorCredentials, deletion_router
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.voice.domain import Span
from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import Projection, hindsight_client
from tests.it1.test_deletion_execution import finish_deletion
from tests.ut.test_voice_http import TEXT, span

pytestmark = pytest.mark.it1
projection = projection_tests.projection


def profiles(p: Projection) -> TestClient:
    module = importlib.import_module("digital_souls_memory_hindsight.character_profile.service")
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_character_profile")
    http = importlib.import_module("digital_souls_memory_hindsight.character_profile_http")
    store = PostgresDeletionStore(p.database)
    service = module.CharacterProfileService(
        storage.PostgresCharacterProfileStore(p.database), p.service, store, clock=p.clock
    )
    deletion = DeletionService(store, p.service, clock=p.clock)
    app = FastAPI()
    app.include_router(http.character_profile_router(lambda: service))
    app.include_router(
        deletion_router(lambda: deletion, AuthorCredentials("synthetic-author", "fixture"))
    )
    return TestClient(app)


def base(p: Projection) -> str:
    return f"/characters/{p.characters[0]}/profile"


def seed(p: Projection) -> dict[str, object]:
    for identifier in ("target", "kept"):
        p.post_turn(identifier, text=TEXT)
        p.receipt(identifier, len(TEXT), len(TEXT))
    return {"char_limit": 8, "entries": [span("target", 1, 4), span("kept", 0, 5)]}


def test_real_database_roundtrip_revision_and_rejected_put_preserves_setting(
    projection: Projection,
) -> None:
    p = projection
    body = seed(p)
    with profiles(p) as client:
        first = client.put(base(p), json=body)
        assert first.status_code == 200, first.text
        assert first.json()["revision"] == 1
    with profiles(p) as client:
        assert client.get(base(p)).json() == first.json()
        second = client.put(base(p), json=body)
        assert second.status_code == 200, second.text
        assert second.json()["revision"] == 2
        assert second.json()["entries"] == body["entries"]
        assert client.put(base(p), json={**body, "char_limit": 7}).status_code == 422
        assert client.get(base(p)).json() == second.json()


def test_receipt_correction_filters_response_without_mutating_persisted_setting(
    projection: Projection,
) -> None:
    p = projection
    body = seed(p)
    with profiles(p) as client:
        saved = client.put(base(p), json=body)
        assert saved.status_code == 200, saved.text
        assert client.get(base(p)).json() == saved.json()
        p.receipt("target", 3, len(TEXT))
        assert client.get(base(p)).json() == {**saved.json(), "entries": [span("kept", 0, 5)]}
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT count(*) FROM character_profile_entries"
            ).fetchone() == (2,)
            assert connection.execute("SELECT revision FROM character_profiles").fetchone() == (1,)
        p.receipt("target", 5, len(TEXT))
        assert client.get(base(p)).json() == saved.json()


@pytest.mark.parametrize("status", ["accepted", "processing", "failed"])
def test_unfinished_deletion_hides_entries_and_rejects_put(
    projection: Projection, status: str
) -> None:
    p = projection
    body = seed(p)
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with profiles(p) as client:
        saved = client.put(base(p), json=body)
        assert saved.status_code == 200, saved.text
        service.accept("delete-target", "synthetic-author", ("target",), None)
        if status != "accepted":
            service.transition("delete-target", "processing")
        if status == "failed":
            service.transition("delete-target", "failed", error="synthetic_failure")
        assert client.get(base(p)).json() == {**saved.json(), "entries": [span("kept", 0, 5)]}
        assert client.put(base(p), json=body).status_code == 422
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT count(*) FROM character_profile_entries"
            ).fetchone() == (2,)
            assert connection.execute("SELECT revision FROM character_profiles").fetchone() == (1,)


def test_author_deletion_removes_only_source_entries_and_records_new_revision(
    projection: Projection,
) -> None:
    p = projection
    body = seed(p)
    # Two removed entries must still cause just one revision increment per profile.
    body["entries"] = [span("target", 0, 1), span("target", 2, 4), span("kept", 0, 5)]
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with profiles(p) as client:
        assert client.put(base(p), json=body).status_code == 200
        unaffected = base(p).replace(p.characters[0], p.characters[1])
        assert client.put(unaffected, json={"char_limit": 0, "entries": []}).status_code == 200

        async def exercise() -> None:
            async with hindsight_client() as hindsight:
                await p.drain(hindsight)
                response = client.post(
                    "/deletion-requests",
                    headers={"Authorization": "Bearer fixture"},
                    json={"request_id": "delete-profile", "target_turn_ids": ["target"]},
                )
                assert response.status_code == 202, response.text
                await finish_deletion(p, hindsight, service, store, "delete-profile")

        asyncio.run(exercise())
        response = client.get(
            "/deletion-requests/delete-profile", headers={"Authorization": "Bearer fixture"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["cascade_result"]["deleted_profile_entries"] == [
            {
                "character_id": p.characters[0],
                "turn_id": "target",
                "start": 0,
                "end": 1,
                "revision": 2,
            },
            {
                "character_id": p.characters[0],
                "turn_id": "target",
                "start": 2,
                "end": 4,
                "revision": 2,
            },
        ]
        profile = client.get(base(p)).json()
        assert profile["revision"] == 2
        assert profile["entries"] == [span("kept", 0, 5)]
        assert profile["char_limit"] == 8
        assert client.get(unaffected).json()["revision"] == 1
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT turn_id FROM character_profile_entries"
            ).fetchall() == [("kept",)]


def test_put_rechecks_source_after_concurrent_deletion(projection: Projection) -> None:
    p = projection
    body = seed(p)
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_character_profile")
    reached, release = Event(), Event()
    original_save = storage.PostgresCharacterProfileStore.save

    def delayed_save(
        self: object,
        character_id: str,
        char_limit: int,
        entries: tuple[Span, ...],
        updated_at: datetime,
    ) -> object:
        reached.set()
        assert release.wait(15), "Deletion did not release PUT"
        return original_save(self, character_id, char_limit, entries, updated_at)

    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with profiles(p) as client, ThreadPoolExecutor(max_workers=1) as executor:
        with patch.object(storage.PostgresCharacterProfileStore, "save", delayed_save):
            pending = executor.submit(client.put, base(p), json=body)
            try:
                assert reached.wait(10), "PUT did not reach persistence"
                request = service.accept("race", "synthetic-author", ("target",), None)
                item = store.claim(p.clock.now, timedelta(seconds=60))
                assert item is not None
                assert store.delete_turns(item, request, p.clock.now)["ledger_done"] is True
            finally:
                release.set()
            response = pending.result(timeout=15)
        assert response.status_code == 422, response.text
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT count(*) FROM character_profile_entries"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT turn_id FROM turn_tombstones WHERE turn_id='target'"
            ).fetchone() == ("target",)


def test_deletion_waits_for_inflight_put_then_cascades(projection: Projection) -> None:
    p = projection
    body = seed(p)
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        connection.execute("""
            CREATE FUNCTION pause_profile_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_advisory_xact_lock(47); RETURN NEW; END; $$;
            CREATE TRIGGER pause_profile BEFORE INSERT ON character_profile_entries
                FOR EACH ROW EXECUTE FUNCTION pause_profile_insert();
        """)
    with (
        profiles(p) as client,
        psycopg.connect(p.database, connect_timeout=5, autocommit=True) as gate,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        gate.execute("SELECT pg_advisory_lock(47)")
        pending = executor.submit(client.put, base(p), json=body)
        try:
            deadline = time.monotonic() + 10
            while True:
                row = gate.execute(
                    "SELECT pid FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                    "AND objid=47 AND database=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database())"
                ).fetchone()
                if row is not None:
                    writer_pid = row[0]
                    break
                assert not pending.done(), "PUT terminated before INSERT"
                assert time.monotonic() < deadline, "PUT did not reach INSERT"
                time.sleep(0.01)
            accepted = executor.submit(
                service.accept, "race-insert", "synthetic-author", ("target",), None
            )
            while True:
                blocked = gate.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                    (writer_pid,),
                ).fetchone()
                if blocked is not None:
                    break
                assert not accepted.done(), "Deletion acceptance bypassed source locks"
                assert time.monotonic() < deadline, "Deletion did not wait for PUT"
                time.sleep(0.01)
        finally:
            gate.execute("SELECT pg_advisory_unlock(47)")
        response = pending.result(timeout=15)
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 1
        request = accepted.result(timeout=15)
        item = store.claim(p.clock.now, timedelta(seconds=60))
        assert item is not None
        progress = store.delete_turns(item, request, p.clock.now)
        assert progress["deleted_profile_entries"] == [
            {
                "character_id": p.characters[0],
                "turn_id": "target",
                "start": 1,
                "end": 4,
                "revision": 2,
            }
        ]
        assert client.get(base(p)).json()["entries"] == [span("kept", 0, 5)]
        assert client.get(base(p)).json()["revision"] == 2
        assert gate.execute("SELECT turn_id FROM character_profile_entries").fetchall() == [
            ("kept",)
        ]
