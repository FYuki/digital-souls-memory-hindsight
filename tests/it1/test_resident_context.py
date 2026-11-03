"""Resident HTTP responses, records and deletion interleavings on PostgreSQL."""

import asyncio
import importlib
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.evidence.projection import bank_id
from digital_souls_memory_hindsight.identity.ports import CoreIdentity, Temperament
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_identity import _IdentityRows
from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import Projection, hindsight_client, wait_idle
from tests.it1.test_deletion_execution import finish_deletion
from tests.it1.test_recall_api import recall
from tests.it1.test_voice_exemplars import seed_qa
from tests.ut.test_evidence_http import session_body
from tests.ut.test_identity_http import seed_body
from tests.ut.test_voice_http import TEXT, span, voice_body

pytestmark = pytest.mark.it1
projection = projection_tests.projection


class Gateway:
    def render_personality(
        self, aspects: dict[str, float], temperament: Temperament, renderer_version: str
    ) -> str:
        return "架空の人格の記述"


def residents(p: Projection, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", p.database)
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", json.dumps(p.characters))
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    worker = MagicMock()
    worker.run = AsyncMock()
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=worker))
    return TestClient(module.create_app(llm_gateway=Gateway()))


def base(p: Projection) -> str:
    return f"/characters/{p.characters[0]}"


def provide(client: TestClient, p: Projection) -> dict[str, object]:
    response = client.post(base(p) + "/resident", json={"session_id": p.session, "surface": "Web"})
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json())


def saved(p: Projection, identifier: object) -> dict[str, object]:
    with psycopg.connect(p.database, row_factory=dict_row, connect_timeout=5) as connection:
        row = connection.execute(
            "SELECT * FROM provision_records WHERE provision_id=%s", (identifier,)
        ).fetchone()
    assert row is not None
    return row


def assert_record(p: Projection, value: dict[str, object]) -> None:
    row = saved(p, value["provision_id"])
    assert row["kind"] == "resident"
    assert row["turn_id"] is None
    assert (row["character_id"], row["session_id"], row["surface"]) == (
        p.characters[0],
        p.session,
        "Web",
    )
    for field, column in (
        ("core_identity", "core_identity_version"),
        ("temperament", "temperament_version"),
        ("personality_profile", "personality_version"),
    ):
        identity = value[field]
        assert row[column] == (
            None if identity is None else cast(dict[str, object], identity)["version"]
        )
    voices = cast(list[dict[str, object]], value["voice_exemplars"])
    assert row["exemplar_ids"] == [v["exemplar_id"] for v in voices]
    profile = cast(dict[str, object] | None, value["character_profile"])
    assert row["profile_revision"] == (None if profile is None else profile["revision"])
    entries = [] if profile is None else cast(list[dict[str, object]], profile["entries"])
    assert row["profile_entries"] == [
        {k: e[k] for k in ("turn_id", "start", "end")} for e in entries
    ]
    assert isinstance(row["processing_time_ms"], int) and row["processing_time_ms"] >= 0
    assert row["created_at"] is not None
    assert row["memory_ids"] == row["source_spans"] == row["verbatim_anchor_ids"] == []


def register_context(client: TestClient, p: Projection) -> None:
    qa = seed_qa(p)
    assert client.post(base(p) + "/voice-exemplars", json=qa).status_code == 201
    assert (
        client.post(base(p) + "/voice-exemplars", json=voice_body(exemplar_id="seed")).status_code
        == 201
    )
    assert (
        client.put(
            base(p) + "/profile", json={"char_limit": 5, "entries": [span("q", 0, 5)]}
        ).status_code
        == 200
    )
    assert (
        client.post(
            base(p) + "/core-identities", json={"values": ["約束"], "prohibitions": ["攻撃"]}
        ).status_code
        == 201
    )
    assert (
        client.post(base(p) + "/temperaments", json={"description": "旧版の架空の気質"}).status_code
        == 201
    )
    assert (
        client.post(base(p) + "/personality-profiles/author-seed", json=seed_body()).status_code
        == 201
    )


def test_snapshot_keeps_concurrent_registration_for_next_provision_in_same_session(
    projection: Projection, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = projection
    reached, release = Event(), Event()
    original = _IdentityRows.get_core

    def paused_core(self: _IdentityRows, version: int | None) -> CoreIdentity | None:
        result = original(self, version)
        if not reached.is_set():
            reached.set()
            assert release.wait(15), "Identity writer did not release snapshot"
        return result

    with residents(p, monkeypatch) as client:
        register_context(client, p)
        before = provide(client, p)
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(_IdentityRows, "get_core", paused_core),
        ):
            pending = executor.submit(provide, client, p)
            try:
                assert reached.wait(10), "Resident did not reach the identity read"
                assert (
                    client.post(
                        base(p) + "/core-identities", json={"values": ["新版"], "prohibitions": []}
                    ).status_code
                    == 201
                )
                assert (
                    client.post(
                        base(p) + "/temperaments", json={"description": "新版の架空の気質"}
                    ).status_code
                    == 201
                )
                assert (
                    client.put(
                        base(p) + "/profile", json={"char_limit": 3, "entries": [span("a", 1, 4)]}
                    ).status_code
                    == 200
                )
                assert (
                    client.post(
                        base(p) + "/voice-exemplars", json=voice_body(exemplar_id="new")
                    ).status_code
                    == 201
                )
            finally:
                release.set()
            during = pending.result(timeout=15)
        after = provide(client, p)
    for key in (
        "core_identity",
        "temperament",
        "personality_profile",
        "voice_exemplars",
        "character_profile",
    ):
        assert during[key] == before[key]
    assert after["core_identity"] == {"version": 2, "values": ["新版"], "prohibitions": []}
    assert after["temperament"] == {"version": 2, "description": "新版の架空の気質"}
    assert after["character_profile"] == {
        "revision": 2,
        "entries": [{**span("a", 1, 4), "text": "😀b🍵"}],
    }
    assert [v["exemplar_id"] for v in cast(list[dict[str, object]], after["voice_exemplars"])] == [
        "new",
        "seed",
        "voice-1",
    ]
    for value in (before, during, after):
        assert_record(p, value)


@pytest.mark.parametrize("status", ["accepted", "processing", "failed"])
def test_unfinished_deletion_filters_resident_response_and_record(
    projection: Projection, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    p = projection
    deletion = DeletionService(PostgresDeletionStore(p.database), p.service, clock=p.clock)
    with residents(p, monkeypatch) as client:
        register_context(client, p)
        provide(client, p)
        deletion.accept("delete-q", "synthetic-author", ("q",), None)
        if status != "accepted":
            deletion.transition("delete-q", "processing")
        if status == "failed":
            deletion.transition("delete-q", "failed", error="synthetic_failure")
        value = provide(client, p)
    assert [v["exemplar_id"] for v in cast(list[dict[str, object]], value["voice_exemplars"])] == [
        "seed"
    ]
    assert value["character_profile"] == {"revision": 1, "entries": []}
    assert_record(p, value)


@pytest.mark.parametrize("erase", [False, True])
def test_acceptance_between_read_and_commit_reselects_both_response_and_record(
    projection: Projection, monkeypatch: pytest.MonkeyPatch, erase: bool
) -> None:
    p = projection
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_resident")
    reached, release = Event(), Event()
    original = storage.PostgresResidentStore.add

    def delayed_add(self: object, value: object, source_turn_ids: frozenset[str]) -> None:
        if not reached.is_set():
            reached.set()
            assert release.wait(15), "Deletion did not release provision"
        original(self, value, source_turn_ids)

    deletion_store = PostgresDeletionStore(p.database)
    deletion = DeletionService(deletion_store, p.service, clock=p.clock)
    with residents(p, monkeypatch) as client:
        register_context(client, p)
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(storage.PostgresResidentStore, "add", delayed_add),
        ):
            pending = executor.submit(provide, client, p)
            try:
                assert reached.wait(10), "Resident did not reach persistence"
                request = deletion.accept("race", "synthetic-author", ("q",), None)
                if erase:
                    item = deletion_store.claim(p.clock.now, timedelta(seconds=60))
                    assert item is not None
                    progress = deletion_store.delete_turns(item, request, p.clock.now)
                    assert progress["ledger_done"] is True
            finally:
                release.set()
            value = pending.result(timeout=15)
    assert value["character_profile"] == {"revision": 2 if erase else 1, "entries": []}
    assert [v["exemplar_id"] for v in cast(list[dict[str, object]], value["voice_exemplars"])] == [
        "seed"
    ]
    assert_record(p, value)
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM provision_records").fetchone() == (1,)


def test_missing_session_does_not_create_an_additional_record(
    projection: Projection, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = projection
    with residents(p, monkeypatch) as client:
        provide(client, p)
        response = client.post(
            base(p) + "/resident", json={"session_id": "missing", "surface": "Web"}
        )
        assert response.status_code == 404
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM provision_records").fetchone() == (1,)


def wait_for_lock[T](
    gate: psycopg.Connection[tuple[object, ...]], pending: Future[T], key: int
) -> int:
    deadline = time.monotonic() + 10
    while True:
        row = gate.execute(
            "SELECT pid FROM pg_locks WHERE locktype='advisory' AND NOT granted AND objid=%s "
            "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())",
            (key,),
        ).fetchone()
        if row is not None:
            return cast(int, row[0])
        assert not pending.done(), "Operation terminated before INSERT gate"
        assert time.monotonic() < deadline, "Operation did not reach INSERT"
        time.sleep(0.01)


def wait_for_blocker[T](
    gate: psycopg.Connection[tuple[object, ...]], pending: Future[T], pid: int
) -> None:
    deadline = time.monotonic() + 10
    while (
        gate.execute(
            "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))", (pid,)
        ).fetchone()
        is None
    ):
        assert not pending.done(), "Operation bypassed the source lock"
        assert time.monotonic() < deadline, "Operation did not wait for source lock"
        time.sleep(0.01)


@pytest.mark.parametrize("first", ["resident", "deletion"])
def test_source_locks_serialize_acceptance_and_provision_commit(
    projection: Projection, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    p = projection
    deletion = DeletionService(PostgresDeletionStore(p.database), p.service, clock=p.clock)
    table = "provision_records" if first == "resident" else "deletion_requests"
    with residents(p, monkeypatch) as client:
        register_context(client, p)
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            connection.execute(
                "CREATE FUNCTION pause_resident_race() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM pg_advisory_xact_lock(49); RETURN NEW; END; $$; "
                f"CREATE TRIGGER pause_resident BEFORE INSERT ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION pause_resident_race();"
            )
        with (
            psycopg.connect(p.database, connect_timeout=5, autocommit=True) as gate,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            gate.execute("SELECT pg_advisory_lock(49)")
            try:
                if first == "resident":
                    provision = executor.submit(provide, client, p)
                    pid = wait_for_lock(gate, provision, 49)
                    accepted = executor.submit(
                        deletion.accept, "lock-race", "synthetic-author", ("q",), None
                    )
                    wait_for_blocker(gate, accepted, pid)
                    assert not provision.done(), "HTTP success preceded commit"
                else:
                    accepted = executor.submit(
                        deletion.accept, "lock-race", "synthetic-author", ("q",), None
                    )
                    pid = wait_for_lock(gate, accepted, 49)
                    provision = executor.submit(provide, client, p)
                    wait_for_blocker(gate, provision, pid)
            finally:
                gate.execute("SELECT pg_advisory_unlock(49)")
            accepted.result(timeout=15)
            value = provision.result(timeout=15)
        assert_record(p, value)
        ids = [v["exemplar_id"] for v in cast(list[dict[str, object]], value["voice_exemplars"])]
        assert ids == (["seed", "voice-1"] if first == "resident" else ["seed"])
        next_value = provide(client, p)
        assert next_value["character_profile"] == {"revision": 1, "entries": []}


@pytest.mark.parametrize("source", ["question", "answer", "profile"])
def test_deletion_cascades_resident_record_nulls_recall_reference_and_never_resurrects(
    projection: Projection, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    p = projection
    store = PostgresDeletionStore(p.database)
    deletion = DeletionService(store, p.service, clock=p.clock)
    with residents(p, monkeypatch) as client:
        # A record without deleted provenance must survive the same deletion.
        unaffected = provide(client, p)
        qa = seed_qa(p)
        if source == "profile":
            assert (
                client.put(
                    base(p) + "/profile", json={"char_limit": 5, "entries": [span("q", 0, 5)]}
                ).status_code
                == 200
            )
        else:
            assert client.post(base(p) + "/voice-exemplars", json=qa).status_code == 201
        target = "a" if source == "answer" else "q"
        resident = provide(client, p)

        async def exercise() -> str:
            async with hindsight_client() as hindsight:
                await p.drain(hindsight)
                await wait_idle(hindsight, bank_id(p.characters[0]))
                response = await recall(
                    p,
                    hindsight,
                    p.characters[0],
                    "コーヒー",
                    min_reranker_score=0.5,
                    resident_provision_id=resident["provision_id"],
                )
                assert response.status_code == 200, response.text
                assert response.json()["source_spans"] == []
                identifier = str(response.json()["provision_id"])
                assert saved(p, identifier)["resident_provision_id"] == resident["provision_id"]
                deletion.accept("cascade", "synthetic-author", (target,), None)
                await finish_deletion(p, hindsight, deletion, store, "cascade")
                return identifier

        referring = asyncio.run(exercise())
        result = deletion.get("cascade").cascade_result
        assert result is not None
        assert resident["provision_id"] in cast(list[object], result["deleted_provision_ids"])
        assert unaffected["provision_id"] not in cast(list[object], result["deleted_provision_ids"])
        assert saved(p, referring)["resident_provision_id"] is None
        assert saved(p, unaffected["provision_id"])["kind"] == "resident"
        with psycopg.connect(p.database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT count(*) FROM provision_records WHERE provision_id=%s",
                (resident["provision_id"],),
            ).fetchone() == (0,)
        # A new provide on the same application/session cannot restore deleted content.
        value = provide(client, p)
        assert value["voice_exemplars"] == []
        if source == "profile":
            assert value["character_profile"] == {"revision": 2, "entries": []}
        assert_record(p, value)


def test_api_created_resident_is_joinable_from_recall_and_other_session_is_rejected(
    projection: Projection, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = projection
    p.post_turn("source", text=TEXT)
    with residents(p, monkeypatch) as client:
        value = provide(client, p)
        assert (
            client.post("/sessions", json=session_body(session_id="other-session")).status_code
            == 201
        )
        other = client.post(
            base(p) + "/resident", json={"session_id": "other-session", "surface": "Web"}
        )
        assert other.status_code == 200

    async def exercise() -> str:
        async with hindsight_client() as hindsight:
            await p.drain(hindsight)
            good = await recall(
                p,
                hindsight,
                p.characters[0],
                "コーヒー",
                resident_provision_id=value["provision_id"],
            )
            assert good.status_code == 200, good.text
            bad = await recall(
                p,
                hindsight,
                p.characters[0],
                "コーヒー",
                resident_provision_id=other.json()["provision_id"],
            )
            assert bad.status_code == 422, bad.text
            return str(good.json()["provision_id"])

    identifier = asyncio.run(exercise())
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT r.provision_id, t.temperament_version, t.personality_version "
            "FROM provision_records t JOIN provision_records r "
            "ON t.resident_provision_id=r.provision_id WHERE t.provision_id=%s",
            (identifier,),
        ).fetchone() == (value["provision_id"], None, None)
        assert connection.execute("SELECT count(*) FROM provision_records").fetchone() == (3,)
