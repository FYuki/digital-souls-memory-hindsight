"""Real ledger persistence and fixture-backed recall after author deletion acceptance."""

import asyncio
import importlib

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import MOON, TEA, Projection, hindsight_client
from tests.it1.test_verbatim_anchors import anchors, register
from tests.ut.test_deletion_http import AUTH
from tests.ut.test_evidence_http import session_body

pytestmark = pytest.mark.it1
projection = projection_tests.projection


def test_acceptance_persists_snapshot_and_missing_turn_does_not_create_request(
    projection: Projection,
) -> None:
    p = projection
    p.post_turn("actor", actor="synthetic-user", perceived_by=[p.characters[0]])
    p.post_turn("addressed", addressees=["synthetic-user"], address_mode="direct")
    p.post_turn("unrelated")
    service = importlib.import_module("digital_souls_memory_hindsight.deletion.service")
    storage = importlib.import_module("digital_souls_memory_hindsight.postgres_deletion")
    http = importlib.import_module("digital_souls_memory_hindsight.deletion_http")
    store = storage.PostgresDeletionStore(p.database)
    instance = service.DeletionService(store, p.service, clock=p.clock)
    app = FastAPI()
    app.include_router(
        http.deletion_router(
            lambda: instance, http.AuthorCredentials("synthetic-author", "synthetic-deletion-token")
        )
    )
    with TestClient(app) as client:
        body = {"request_id": "d", "user_id": "synthetic-user"}
        first = client.post("/deletion-requests", headers=AUTH, json=body)
        assert first.status_code == 202, first.text
        assert first.json()["target_turn_ids"] == ["actor", "addressed"]
        # Rebuild the store/service to ensure GET reads committed PostgreSQL state.
        instance = service.DeletionService(
            storage.PostgresDeletionStore(p.database), p.service, clock=p.clock
        )
        assert client.get("/deletion-requests/d", headers=AUTH).json() == first.json()
        p.post_turn("later", actor="synthetic-user", perceived_by=[p.characters[0]])
        replay = client.post("/deletion-requests", headers=AUTH, json=body)
        assert replay.status_code == 202
        assert replay.json() == first.json()
        invalid = client.post(
            "/deletion-requests",
            headers=AUTH,
            json={"request_id": "missing", "target_turn_ids": ["actor", "absent"]},
        )
        assert invalid.status_code == 422
        assert client.get("/deletion-requests/missing", headers=AUTH).status_code == 404
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute("SELECT request_id FROM deletion_requests").fetchall() == [("d",)]
    processing = instance.transition("d", "processing")
    ports = importlib.import_module("digital_souls_memory_hindsight.deletion.ports")
    with pytest.raises(ports.DeletionTransitionRejected):
        store.save_transition("accepted", processing)
    assert store.get("d").status == "processing"
    assert store.blocked_sessions(p.characters[0]) == frozenset({p.session})
    instance.transition("d", "completed")
    assert store.get("d").status == "completed"
    assert store.blocked_sessions(p.characters[0]) == frozenset()
    with (
        pytest.raises(psycopg.errors.CheckViolation),
        psycopg.connect(p.database, connect_timeout=5) as connection,
    ):
        connection.execute("UPDATE deletion_requests SET status = 'unknown' WHERE request_id = 'd'")
    assert store.get("d").status == "completed"


def test_same_recall_service_blocks_session_memories_anchors_and_provisions(
    projection: Projection,
) -> None:
    p = projection
    a, b, c = p.characters
    p.post_turn("target", text=TEA, perceived_by=[a, c])
    p.receipt("target", len(TEA), len(TEA))
    p.post_turn("same-session", text=MOON, perceived_by=[a, b, c])
    p.receipt("same-session", len(MOON), len(MOON))
    other_session = p.session + "-other"
    assert p.http.post("/sessions", json=session_body(session_id=other_session)).status_code == 201
    p.post_turn("kept", session_id=other_session, text=MOON)
    with anchors(p) as client:
        assert register(client, a, "target-anchor", "target", "commitment").status_code == 201
        assert (
            register(client, a, "session-anchor", "same-session", "commitment").status_code == 201
        )

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            deletion = importlib.import_module("digital_souls_memory_hindsight.deletion.service")
            storage = importlib.import_module("digital_souls_memory_hindsight.postgres_deletion")
            deletion_http = importlib.import_module("digital_souls_memory_hindsight.deletion_http")
            recall = importlib.import_module("digital_souls_memory_hindsight.recall.service")
            recall_http = importlib.import_module("digital_souls_memory_hindsight.recall_http")
            adapter = importlib.import_module("digital_souls_memory_hindsight.hindsight_recall")
            provision = importlib.import_module("digital_souls_memory_hindsight.postgres_provision")
            anchor = importlib.import_module("digital_souls_memory_hindsight.anchors.service")
            anchor_store = importlib.import_module(
                "digital_souls_memory_hindsight.postgres_anchors"
            )
            deletions = deletion.DeletionService(
                storage.PostgresDeletionStore(p.database), p.service
            )
            instance = recall.RecallService(
                p.service,
                adapter.HindsightRecall(client),
                provision.PostgresProvisionStore(p.database),
                anchor.AnchorService(anchor_store.PostgresAnchorStore(p.database), p.service),
                deletions,
            )
            app = FastAPI()
            app.include_router(recall_http.recall_router(lambda: instance))
            app.include_router(
                deletion_http.deletion_router(
                    lambda: deletions,
                    deletion_http.AuthorCredentials("synthetic-author", "synthetic-deletion-token"),
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://service"
            ) as transport:

                async def observe(character: str) -> tuple[set[str], set[str], set[str]]:
                    response = await transport.post(
                        f"/characters/{character}/recall",
                        json={
                            "session_id": p.session,
                            "format": "assembled",
                            "query": "架空の翡翠茶と月",
                            "sensitivity": "public",
                            "classifier_version": "fixture-1",
                        },
                    )
                    assert response.status_code == 200, response.text
                    body = response.json()
                    with psycopg.connect(
                        p.database, row_factory=dict_row, connect_timeout=5
                    ) as conn:
                        row = conn.execute(
                            "SELECT memory_ids, source_spans, verbatim_anchor_ids "
                            "FROM provision_records WHERE provision_id=%s",
                            (body["provision_id"],),
                        ).fetchone()
                    assert row is not None
                    assert row["memory_ids"] == [m["id"] for m in body["memories"]]
                    assert row["source_spans"] == [
                        {key: s[key] for key in ("turn_id", "start", "end")}
                        for s in body["source_spans"]
                    ]
                    assert row["verbatim_anchor_ids"] == [
                        a["anchor_id"] for a in body["verbatim_anchors"]
                    ]
                    assert body["context"] == "\n".join(s["text"] for s in body["source_spans"])
                    return (
                        {m["id"] for m in body["memories"]},
                        {s["turn_id"] for s in body["source_spans"]},
                        {a["anchor_id"] for a in body["verbatim_anchors"]},
                    )

                before = {character: await observe(character) for character in (a, b, c)}
                assert before[a][0] and {"target", "same-session", "kept"} <= before[a][1]
                assert before[a][2] == {"target-anchor", "session-anchor"}
                assert before[b][1] == {"same-session"}
                assert {"target", "same-session"} <= before[c][1]
                accepted = await transport.post(
                    "/deletion-requests",
                    headers=AUTH,
                    json={"request_id": "d", "target_turn_ids": ["target"]},
                )
                assert accepted.status_code == 202, accepted.text
                for state in ("accepted", "processing", "failed"):
                    if state != "accepted":
                        deletions.transition(
                            "d", state, error="synthetic failure" if state == "failed" else None
                        )
                    after_a = await observe(a)
                    assert after_a[0] and after_a[0] <= before[a][0]
                    assert after_a[1] == {"kept"}
                    assert after_a[2] == set()
                    assert await observe(c) == (set(), set(), set())
                    assert await observe(b) == before[b]

    asyncio.run(exercise())
