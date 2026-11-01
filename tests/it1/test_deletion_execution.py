"""Author deletion through the durable worker and pinned fixture-backed Hindsight."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import NotFoundException
from psycopg.types.json import Jsonb

from digital_souls_memory_hindsight.deletion.domain import DeletionLeaseLost
from digital_souls_memory_hindsight.deletion.execution import DeletionExecutor
from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.evidence.projection import bank_id, document_id
from digital_souls_memory_hindsight.hindsight_deletion import HindsightDeletion
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.worker import Worker
from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import (
    COFFEE,
    SECRET,
    TEA,
    Projection,
    assert_no_private_tokens,
    assert_search_isolated,
    document_text,
    hindsight_client,
    wait_idle,
)
from tests.it1.test_recall_api import recall
from tests.it1.test_verbatim_anchors import anchors, register
from tests.ut.test_evidence_http import turn_body

pytestmark = pytest.mark.it1
projection = projection_tests.projection


async def finish_deletion(
    p: Projection,
    client: Hindsight,
    service: DeletionService,
    store: PostgresDeletionStore,
    request_id: str,
) -> None:
    worker = Worker(
        PostgresOutbox(p.database),
        HindsightRetain(client),
        p.service,
        clock=p.clock,
        deletion=DeletionExecutor(service, store, HindsightDeletion(client), clock=p.clock),
    )
    deadline = asyncio.get_running_loop().time() + 90
    while True:
        await worker.run_once()
        request = service.get(request_id)
        assert request.status != "failed", request.error
        if request.status == "completed":
            return
        assert asyncio.get_running_loop().time() < deadline, request.status
        p.clock.now += timedelta(seconds=1)
        await asyncio.sleep(0.1)


def add_memory_provision(p: Projection, identifier: str, character: str, memory_id: str) -> None:
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO provision_records (provision_id, kind, character_id, session_id, "
            "surface, turn_id, memory_ids, source_spans, verbatim_anchor_ids, created_at, "
            "memory_trust, source_span_trust, verbatim_anchor_trust) "
            "VALUES (%s,'turn',%s,%s,'Web','query-turn',%s,%s,%s,%s,%s,%s,%s)",
            (
                identifier,
                character,
                p.session,
                [memory_id],
                Jsonb([]),
                [],
                p.clock.now,
                ["inferred"],
                [],
                [],
            ),
        )


def test_partial_deletion_rebuilds_both_banks_and_removes_anchors_and_provisions(
    projection: Projection,
) -> None:
    p = projection
    a, b, c = p.characters
    target_text = SECRET + "\n" + TEA
    p.post_turn("target", text=target_text, perceived_by=[a, b])
    p.receipt("target", len(target_text), len(target_text))
    remaining = p.post_turn(
        "kept", text=TEA + "\n" + COFFEE, perceived_by=[a, b], reply_to_turn_id="target"
    )
    p.receipt("kept", len(TEA), len(TEA + "\n" + COFFEE))
    p.post_turn("other-support", text=TEA, perceived_by=[a, b])
    p.receipt("other-support", len(TEA), len(TEA))
    with anchors(p) as transport:
        for character in (a, b):
            assert (
                register(transport, character, "deleted-anchor", "target", "commitment").status_code
                == 201
            )
            assert (
                register(transport, character, "kept-anchor", "kept", "commitment").status_code
                == 201
            )
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            old_provisions: set[str] = set()
            for character in (a, b):
                await wait_idle(client, bank_id(character))
                target = (bank_id(character), document_id(p.session))
                assert SECRET in await document_text(client, target)
                before = await recall(p, client, character, "金庫 翡翠茶")
                assert before.status_code == 200, before.text
                old_provisions.add(before.json()["provision_id"])
                memories = await client.memory.list_memories(bank_id=bank_id(character), limit=100)
                target_memories = [
                    item
                    for item in memories.items
                    if "七九三" in (item.text or "") and item.fact_type != "observation"
                ]
                assert target_memories, memories.to_dict()
                identifier = "memory-only-" + character
                add_memory_provision(p, identifier, character, target_memories[0].id)
                old_provisions.add(identifier)
                add_memory_provision(
                    p, "unrelated-" + character, character, "absent-before-deletion"
                )
            add_memory_provision(p, "other-character", c, "absent-before-deletion")
            service.accept("d", "synthetic-author", ("target",), None)
            service.accept("d", "synthetic-author", ("target",), None)
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM deletion_outbox WHERE request_id='d'"
                ).fetchone() == (1,)
            await finish_deletion(p, client, service, store, "d")
            result = service.get("d").cascade_result
            assert result is not None
            assert result["deleted_turn_ids"] == ["target"]
            assert result["rebuilt_documents"] == [
                {"bank_id": bank_id(character), "document_id": document_id(p.session)}
                for character in sorted((a, b))
            ]
            assert result["deleted_documents"] == []
            assert result["deleted_anchors"] == [
                {"character_id": character, "anchor_id": "deleted-anchor"}
                for character in sorted((a, b))
            ]
            deleted_provisions = result["deleted_provision_ids"]
            assert isinstance(deleted_provisions, list)
            assert old_provisions <= set(deleted_provisions)
            for character in (a, b):
                target = (bank_id(character), document_id(p.session))
                await assert_search_isolated(client, target, "翡翠茶", ("金庫", "七九三"))
                response = await recall(p, client, character, "翡翠茶")
                assert response.status_code == 200, response.text
                assert response.json()["memories"]
                assert_no_private_tokens(response.json(), ("金庫", "七九三"))
                enumerated = await client.memory.list_memories(
                    bank_id=bank_id(character), limit=100
                )
                assert any(item.fact_type == "observation" for item in enumerated.items), (
                    enumerated.to_dict()
                )
                assert_no_private_tokens(enumerated.to_dict(), ("金庫", "七九三"))
                if character == b:
                    assert COFFEE not in await document_text(client, target)
            assert p.http.get("/turns/kept").json() == remaining
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                for table in ("turns", "delivery_receipts", "delivery_scopes", "outbox"):
                    assert connection.execute(
                        f"SELECT count(*) FROM {table} WHERE turn_id='target'"
                    ).fetchone() == (0,)
                tombstone = connection.execute(
                    "SELECT turn_id, request_id, deleted_at FROM turn_tombstones"
                ).fetchone()
                assert (
                    tombstone is not None
                    and tombstone[:2] == ("target", "d")
                    and tombstone[2] is not None
                )
                surviving = {
                    row[0]
                    for row in connection.execute("SELECT provision_id FROM provision_records")
                }
                assert not old_provisions & surviving
                assert {"other-character", "unrelated-" + a, "unrelated-" + b} <= surviving
                assert connection.execute(
                    "SELECT count(*) FROM verbatim_anchors WHERE anchor_id='kept-anchor'"
                ).fetchone() == (2,)
            assert_no_private_tokens(dict(result), ("金庫", "七九三"))

    asyncio.run(exercise())


def test_user_union_full_deletion_removes_documents_and_rejects_reregistration(
    projection: Projection,
) -> None:
    p = projection
    a, b, _ = p.characters
    p.post_turn("actor", actor="synthetic-user", perceived_by=[a, b], text=TEA)
    p.post_turn(
        "addressed",
        addressees=["synthetic-user"],
        address_mode="direct",
        perceived_by=[a, b],
        text=SECRET,
    )
    p.receipt("addressed", len(SECRET), len(SECRET))
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            for character in (a, b):
                await wait_idle(client, bank_id(character))
                assert TEA in await document_text(
                    client, (bank_id(character), document_id(p.session))
                )
            request = service.accept("all", "synthetic-author", None, "synthetic-user")
            assert request.target_turn_ids == ("actor", "addressed")
            await finish_deletion(p, client, service, store, "all")
            result = service.get("all").cascade_result
            assert result is not None and result["rebuilt_documents"] == []
            assert result["deleted_documents"] == [
                {"bank_id": bank_id(character), "document_id": document_id(p.session)}
                for character in sorted((a, b))
            ]
            for character in (a, b):
                with pytest.raises(NotFoundException):
                    await document_text(client, (bank_id(character), document_id(p.session)))
                recalled = await client.arecall(
                    bank_id=bank_id(character),
                    query="翡翠茶 金庫",
                    include_chunks=True,
                    types=["world", "experience", "observation"],
                )
                assert_no_private_tokens(recalled.to_dict(), ("翡翠茶", "金庫", "七九三"))
            response = p.http.post(
                "/turns",
                json=turn_body(
                    turn_id="actor",
                    session_id=p.session,
                    actor="synthetic-user",
                    perceived_by=[a, b],
                    text=TEA,
                ),
            )
            assert response.status_code == 409
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                assert connection.execute("SELECT count(*) FROM turns").fetchone() == (0,)
                assert connection.execute(
                    "SELECT count(*) FROM outbox WHERE turn_id IS NOT NULL"
                ).fetchone() == (0,)

    asyncio.run(exercise())


def test_ledger_deletion_cancels_pending_payload_and_preserves_progress_on_reentry(
    projection: Projection,
) -> None:
    p = projection
    a = p.characters[0]
    p.post_turn("target", text=SECRET)
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    request = service.accept("pending", "synthetic-author", ("target",), None)
    item = store.claim(p.clock.now, timedelta(seconds=60))
    assert item is not None
    with pytest.raises(DeletionLeaseLost):
        store.save_progress(replace(item, lease_token="stale"), {"ledger_done": True}, p.clock.now)
    with pytest.raises(DeletionLeaseLost):
        store.delete_turns(replace(item, lease_token="stale"), request, p.clock.now)
    assert p.http.get("/turns/target").status_code == 200
    store.save_progress(item, {"memory_snapshot": {a: []}}, p.clock.now)
    first = store.delete_turns(item, request, p.clock.now)
    assert store.delete_turns(item, request, p.clock.now) == first
    rebuild = PostgresOutbox(p.database).claim(p.clock.now, timedelta(seconds=60))
    assert rebuild is not None and rebuild.update_mode == "delete"
    before = p.operation_ids()
    PostgresEvidenceStore(p.database).add_retain(rebuild.operation)
    assert p.operation_ids() == before
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT count(*) FROM outbox WHERE content LIKE %s", ("%" + SECRET + "%",)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM outbox WHERE deletion_request_id='pending'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT progress FROM deletion_outbox WHERE request_id='pending'"
        ).fetchone() == (first,)


def test_deletion_acceptance_rolls_back_if_its_outbox_cannot_be_saved(
    projection: Projection,
) -> None:
    p = projection
    p.post_turn("target", text=SECRET)
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        connection.execute("""
            CREATE FUNCTION reject_deletion_enqueue() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic enqueue failure'; END; $$;
            CREATE TRIGGER reject_deletion_enqueue BEFORE INSERT ON deletion_outbox
                FOR EACH STATEMENT EXECUTE FUNCTION reject_deletion_enqueue();
        """)
    from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable

    with pytest.raises(EvidenceUnavailable):
        service.accept("d", "synthetic-author", ("target",), None)
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM deletion_requests").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM deletion_blocks").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM deletion_outbox").fetchone() == (0,)
        connection.execute("DROP TRIGGER reject_deletion_enqueue ON deletion_outbox")
    assert service.accept("d", "synthetic-author", ("target",), None).status == "accepted"
