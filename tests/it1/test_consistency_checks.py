"""Consistency registration transactions and resident visibility on real PostgreSQL."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from tests.consistency_doubles import Gateway, Reviewer
from tests.ut.test_consistency_checks import ROOT, seed
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import session_body
from tests.ut.test_identity_http import seed_body
from tests.ut.test_voice_http import BASE, TEXT, receipt, seed_turns, turn_voice, voice_body

pytestmark = pytest.mark.it1


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()


@pytest.fixture
def reviewer() -> Reviewer:
    return Reviewer()


@pytest.fixture
def client(
    database: str,
    gateway: Gateway,
    reviewer: Reviewer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> TestClient:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a", "char-b"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_ID", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_TOKEN", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=MagicMock(run=AsyncMock())))
    return TestClient(module.create_app(llm_gateway=gateway, reviewer=reviewer))


def provide(client: TestClient, database: str) -> dict[str, object]:
    response = client.post(ROOT + "/resident", json={"session_id": "s-1", "surface": "Web"})
    assert response.status_code == 200, response.text
    value = response.json()
    with psycopg.connect(database, connect_timeout=5, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM provision_records WHERE provision_id=%s", (value["provision_id"],)
        ).fetchone()
    assert row is not None
    assert row["temperament_version"] == value["temperament"]["version"]
    assert row["personality_version"] == (
        None if value["personality_profile"] is None else value["personality_profile"]["version"]
    )
    assert row["exemplar_ids"] == [v["exemplar_id"] for v in value["voice_exemplars"]]
    return cast(dict[str, object], value)


def exemplar_ids(value: dict[str, object]) -> list[str]:
    voices = cast(list[dict[str, object]], value["voice_exemplars"])
    return [str(v["exemplar_id"]) for v in voices]


@pytest.mark.parametrize("operation", ["seed", "temperament"])
@pytest.mark.parametrize("failure", ["inconsistent", "unavailable"])
def test_rejected_identity_never_reaches_current_resident_or_provision_record(
    client: TestClient, database: str, reviewer: Reviewer, operation: str, failure: str
) -> None:
    with client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        if operation == "temperament":
            seed(client)
        else:
            assert (
                client.post(ROOT + "/temperaments", json={"description": "穏やか"}).status_code
                == 201
            )
        before = client.get(ROOT + "/identity/current").json()
        reviewer.consistent = failure != "inconsistent"
        reviewer.error = RuntimeError("synthetic failure") if failure == "unavailable" else None
        path, body = (
            ("personality-profiles/author-seed", seed_body())
            if operation == "seed"
            else ("temperaments", {"description": "拒否する気質"})
        )
        response = client.post(ROOT + "/" + path, json=body)
        assert response.status_code == (422 if failure == "inconsistent" else 503)
        assert client.get(ROOT + "/identity/current").json() == before
        assert client.get(BASE).json() == []
        value = provide(client, database)
        assert value["temperament"] == {"version": 1, "description": "穏やか"}
        assert value["personality_profile"] == (
            None
            if operation == "seed"
            else {k: before["personality_profile"][k] for k in ("version", "description")}
        )
        assert value["voice_exemplars"] == []


def test_failed_voice_never_reaches_list_resident_or_provision_and_retry_is_first(
    client: TestClient, database: str, gateway: Gateway
) -> None:
    with client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        seed(client)
        gateway.generation_error = RuntimeError("synthetic generation failure")
        assert client.post(BASE, json=voice_body()).status_code == 503
        assert client.get(BASE).json() == []
        assert provide(client, database)["voice_exemplars"] == []
        with psycopg.connect(database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT count(*) FROM voice_consistency_reviews"
            ).fetchone() == (0,)
        gateway.generation_error = None
        response = client.post(BASE, json=voice_body())
        assert response.status_code == 201
        assert response.json()["consistency_review"] is not None
        assert exemplar_ids(provide(client, database)) == ["voice-1"]


def test_review_commit_failure_rolls_back_exemplar_and_review_together(
    client: TestClient, database: str
) -> None:
    with psycopg.connect(database, connect_timeout=5) as connection:
        connection.execute("""
            CREATE FUNCTION reject_review_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic commit failure'; END; $$;
            CREATE CONSTRAINT TRIGGER reject_review AFTER INSERT ON voice_consistency_reviews
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_review_commit();
        """)
    with client:
        assert client.post(BASE, json=voice_body()).status_code == 503
        assert client.get(BASE).json() == []
        with psycopg.connect(database, connect_timeout=5) as connection:
            assert connection.execute("SELECT count(*) FROM voice_exemplars").fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM voice_consistency_reviews"
            ).fetchone() == (0,)
            connection.execute("DROP TRIGGER reject_review ON voice_consistency_reviews")
        retry = client.post(BASE, json=voice_body())
        assert retry.status_code == 201
        assert retry.json()["consistency_review"] is not None
        with psycopg.connect(database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT v.exemplar_id,r.consistent,r.reason FROM voice_exemplars v "
                "JOIN voice_consistency_reviews r USING(character_id,exemplar_id)"
            ).fetchall() == [("voice-1", True, "架空の審査理由")]
            with connection.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute("SELECT * FROM voice_consistency_reviews").fetchone()
            assert row is not None
            aspects = cast(dict[str, float], seed_body()["aspects"])
            assert not ({"aspects"} | set(aspects)) & set(row)


def test_concurrent_initial_registrations_commit_one_result_for_the_winning_exemplar(
    client: TestClient, database: str, gateway: Gateway
) -> None:
    barrier = Barrier(2)
    original = gateway.respond_in_voice

    def generate(situation: str, answer: str, question: str | None, temperament: str | None) -> str:
        barrier.wait(timeout=15)
        return original(situation, answer, question, temperament)

    with (
        client,
        patch.object(gateway, "respond_in_voice", side_effect=generate),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [
            pool.submit(client.post, BASE, json=voice_body(exemplar_id=name))
            for name in ("one", "two")
        ]
        responses = [future.result(timeout=20) for future in futures]
        assert [r.status_code for r in responses] == [201, 201]
        reviewed = [r.json() for r in responses if r.json()["consistency_review"] is not None]
        assert len(reviewed) == 1
        with psycopg.connect(database, connect_timeout=5) as connection:
            assert connection.execute(
                "SELECT character_id,exemplar_id,consistent,reason FROM voice_consistency_reviews"
            ).fetchall() == [("char-a", reviewed[0]["exemplar_id"], True, "架空の審査理由")]
            assert connection.execute("SELECT count(*) FROM voice_exemplars").fetchone() == (2,)


def test_source_deletion_keeps_review_and_recreated_app_does_not_review_again(
    client: TestClient, database: str, gateway: Gateway, reviewer: Reviewer
) -> None:
    with client:
        seed_turns(client)
        receipt(client, "a", len(TEXT))
        assert client.post(BASE, json=turn_voice()).status_code == 201
        with psycopg.connect(database, connect_timeout=5) as connection:
            before = connection.execute("SELECT * FROM voice_consistency_reviews").fetchall()
        store = PostgresDeletionStore(database)
        deletion = DeletionService(
            store, cast(FastAPI, client.app).state.ledger_service, clock=Clock()
        )
        request = deletion.accept("erase-voice", "synthetic-author", ("a",), None)
        item = store.claim(Clock().now, timedelta(seconds=60))
        assert item is not None
        store.delete_turns(item, request, Clock().now)
        assert client.get(BASE).json() == []
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    gateway.generation_error = reviewer.error = RuntimeError("must not review again")
    with TestClient(module.create_app()) as restored:
        response = restored.post(BASE, json=voice_body(exemplar_id="replacement"))
        assert response.status_code == 201
        assert response.json()["consistency_review"] is None
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute("SELECT * FROM voice_consistency_reviews").fetchall() == before
