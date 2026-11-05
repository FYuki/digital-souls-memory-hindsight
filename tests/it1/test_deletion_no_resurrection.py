"""Deletion races, replay and process recovery against the pinned IT1 server."""

import asyncio
import os
import select
import subprocess
import sys
from datetime import datetime, timedelta
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from hindsight_client import Hindsight
from hindsight_client_api.models.memory_unit_list_item import MemoryUnitListItem

from digital_souls_memory_hindsight.deletion.execution import DeletionExecutor
from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.evidence.projection import bank_id, document_id
from digital_souls_memory_hindsight.hindsight_deletion import HindsightDeletion
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.outbox import OutboxItem, RetainOperation, RetainResult
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.worker import Worker
from tests.it1 import test_bank_projection as projection_tests
from tests.it1 import test_recall_api as recall_tests
from tests.it1.conftest import ResponseGate
from tests.it1.test_bank_projection import (
    COFFEE,
    TEA,
    Projection,
    assert_no_private_tokens,
    document_text,
    hindsight_client,
    wait_idle,
)
from tests.it1.test_deletion_execution import finish_deletion
from tests.it1.test_recall_api import recall, wait_consolidated
from tests.it1.test_verbatim_anchors import anchors, register
from tests.ut.test_evidence_http import session_body, turn_body

pytestmark = pytest.mark.it1
projection = projection_tests.projection
consolidation_gate = recall_tests.consolidation_gate

# finish_deletion can wait 90 seconds, including Hindsight's 60-second retry.
# Keep the paused worker alive until deletion finishes and releases it.
STALE_CLAIM_RELEASE_TIMEOUT = 120


def deletion(p: Projection) -> tuple[DeletionService, PostgresDeletionStore]:
    store = PostgresDeletionStore(p.database)
    return DeletionService(store, p.service, clock=p.clock), store


def worker(
    p: Projection, client: Hindsight, service: DeletionService, store: PostgresDeletionStore
) -> Worker:
    return Worker(
        PostgresOutbox(p.database),
        HindsightRetain(client),
        p.service,
        clock=p.clock,
        deletion=DeletionExecutor(service, store, HindsightDeletion(client), clock=p.clock),
    )


async def seed(p: Projection, client: Hindsight) -> None:
    for character in p.characters:
        p.post_turn(
            "seed-" + character, actor=character, perceived_by=[character], kind="thought", text=TEA
        )
    await p.drain(client)
    for character in p.characters:
        await wait_consolidated(client, bank_id(character))


def target(p: Projection, marker: str) -> dict[str, object]:
    saved = p.post_turn(
        "target", text=TEA + " 合成削除本文:" + marker, perceived_by=list(p.characters[:2])
    )
    p.receipt("target", len(str(saved["text"])), len(str(saved["text"])))
    with anchors(p) as transport:
        for character in p.characters[:2]:
            response = register(transport, character, "target-anchor", "target", "commitment")
            assert response.status_code == 201, response.text
    return saved


async def memories(client: Hindsight, bank: str) -> list[MemoryUnitListItem]:
    items: list[MemoryUnitListItem] = []
    while True:
        page = await client.memory.list_memories(bank_id=bank, limit=100, offset=len(items))
        items.extend(page.items)
        if len(items) >= page.total:
            return items
        assert page.items, "Memory listing did not advance"


async def documents(client: Hindsight, bank: str) -> list[str]:
    ids: list[str] = []
    while True:
        page = await client.documents.list_documents(bank_id=bank, limit=100, offset=len(ids))
        ids.extend(item.id for item in page.items)
        if len(ids) >= page.total:
            return ids
        assert page.items, "Document listing did not advance"


async def assert_erased(p: Projection, client: Hindsight, marker: str) -> None:
    for character in p.characters:
        bank = bank_id(character)
        await wait_idle(client, bank)
        await wait_consolidated(client, bank)
        for identifier in await documents(client, bank):
            assert_no_private_tokens(await document_text(client, (bank, identifier)), (marker,))
            offset = 0
            while True:
                chunks = await client.documents.list_document_chunks(
                    bank_id=bank,
                    document_id=identifier,
                    limit=100,
                    offset=offset,
                )
                assert_no_private_tokens(chunks.to_dict(), (marker,))
                offset += len(chunks.items)
                if offset >= chunks.total:
                    break
                assert chunks.items, "Chunk listing did not advance"
        for memory in await memories(client, bank):
            assert_no_private_tokens(memory.to_dict(), (marker,))
        recalled = await client.arecall(
            bank_id=bank,
            query="翡翠茶 合成削除本文",
            include_chunks=True,
            include_source_facts=True,
            types=["world", "experience", "observation"],
        )
        assert_no_private_tokens(recalled.to_dict(), (marker,))
        response = await recall(p, client, character, "翡翠茶")
        assert response.status_code == 200, response.text
        assert response.json()["memories"], response.text
        assert any(
            span["turn_id"] == "seed-" + character for span in response.json()["source_spans"]
        ), response.text
        assert_no_private_tokens(response.json(), (marker,))
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        for table in ("provision_records", "verbatim_anchors"):
            rows = connection.execute(f"SELECT to_jsonb(t) FROM {table} t").fetchall()
            for row in rows:
                assert_no_private_tokens(row[0], (marker,))
        assert connection.execute(
            "SELECT count(*) FROM turns WHERE turn_id='target'"
        ).fetchone() == (0,)


async def assert_waiting(
    p: Projection, client: Hindsight, service: DeletionService, store: PostgresDeletionStore
) -> None:
    await worker(p, client, service, store).run_once()
    assert service.get("d").status == "processing"
    assert p.http.get("/turns/target").status_code == 200


def test_public_api_rebuilds_observations_when_no_individual_delete_is_available(
    projection: Projection,
) -> None:
    p = projection

    async def exercise() -> None:
        async with hindsight_client() as client:
            await seed(p, client)
            bank = bank_id(p.characters[0])
            before = await memories(client, bank)
            observations = [m for m in before if m.fact_type == "observation"]
            facts = [m for m in before if m.fact_type != "observation"]
            assert observations and facts
            result = await client.memory.clear_memory_observations(
                bank_id=bank,
                memory_id=observations[0].id,
            )
            await wait_idle(client, bank)
            assert result.deleted_count == 0
            assert observations[0].id in {m.id for m in await memories(client, bank)}
            adapter = HindsightDeletion(client)
            await adapter.clear_observations(bank)
            assert await adapter.observation_sources(bank) == {}
            for fact in facts:
                await adapter.clear_memory_observations(bank, fact.id)
            await wait_idle(client, bank)
            await wait_consolidated(client, bank)
            rebuilt = await memories(client, bank)
            assert any(m.fact_type == "observation" and TEA in str(m.text) for m in rebuilt)
            assert {m.id for m in facts} <= {m.id for m in rebuilt}
            assert (await document_text(client, (bank, document_id(p.session)))).count(TEA) == 1

    asyncio.run(exercise())


def test_deletion_during_extraction_waits_then_erases_every_surface(
    projection: Projection,
    response_gate: ResponseGate,
) -> None:
    p = projection

    async def exercise() -> None:
        async with hindsight_client() as client:
            await seed(p, client)
            target(p, response_gate.marker)
            service, store = deletion(p)
            try:
                await Worker(
                    PostgresOutbox(p.database), HindsightRetain(client), p.service, clock=p.clock
                ).run_once()
                await response_gate.wait_entered()
                operations = await client.operations.list_operations(
                    bank_id=bank_id(p.characters[0]),
                    type="retain",
                )
                assert any(
                    item.status in {"pending", "processing"} for item in operations.operations
                ), operations.to_dict()
                service.accept("d", "synthetic-author", ("target",), None)
                await assert_waiting(p, client, service, store)
            finally:
                response_gate.release()
                await response_gate.wait_finished()
            await finish_deletion(p, client, service, store, "d")
            await assert_erased(p, client, response_gate.marker)

    asyncio.run(exercise())


def test_deletion_during_multi_source_consolidation_preserves_remaining_support(
    projection: Projection,
    consolidation_gate: ResponseGate,
) -> None:
    p = projection
    marker = "deleted-" + uuid4().hex
    shared = "合成統合キー:" + uuid4().hex

    async def exercise() -> None:
        async with hindsight_client() as client:
            await seed(p, client)
            target(p, marker + " " + shared)
            await p.drain(client)
            a = bank_id(p.characters[0])
            await wait_consolidated(client, a)
            before = [
                m
                for m in await memories(client, a)
                if m.fact_type == "observation" and marker in str(m.text)
            ]
            assert before, "Deleted source must already have an observation"
            other = p.session + "-support"
            from tests.ut.test_evidence_http import session_body

            assert p.http.post("/sessions", json=session_body(session_id=other)).status_code == 201
            p.post_turn(
                "support",
                session_id=other,
                text=TEA + " " + shared + " " + consolidation_gate.marker,
            )
            service, store = deletion(p)
            try:
                await p.drain(client)
                await consolidation_gate.wait_entered()
                operations = await client.operations.list_operations(
                    bank_id=a, type="consolidation"
                )
                assert any(
                    item.status in {"pending", "processing"} for item in operations.operations
                ), operations.to_dict()
                service.accept("d", "synthetic-author", ("target",), None)
                await assert_waiting(p, client, service, store)
            finally:
                consolidation_gate.release()
                await consolidation_gate.wait_finished()
            await wait_consolidated(client, a)
            combined = [
                m
                for m in await memories(client, a)
                if m.fact_type == "observation"
                and marker in str(m.text)
                and consolidation_gate.marker in str(m.text)
            ]
            assert combined and any(len(m.source_memory_ids or []) >= 2 for m in combined), (
                "Fixture must produce an observation supported by both sources"
            )
            await finish_deletion(p, client, service, store, "d")
            await assert_erased(p, client, marker)
            surviving = [
                m
                for m in await memories(client, a)
                if m.fact_type == "observation" and shared in str(m.text)
            ]
            assert surviving, "Remaining support must rebuild the observation"
            assert all(marker not in str(m.text) for m in surviving)

    asyncio.run(exercise())


@pytest.mark.parametrize("replay", ["receipt", "reprocess"])
def test_deleted_turn_cannot_return_after_resend_or_document_reprocessing(
    projection: Projection,
    replay: str,
) -> None:
    p = projection
    marker = "deleted-" + uuid4().hex

    async def exercise() -> None:
        async with hindsight_client() as client:
            await seed(p, client)
            saved = target(p, marker)
            p.post_turn("kept", text=TEA + "\n" + COFFEE, perceived_by=list(p.characters[:2]))
            if replay == "receipt":
                p.receipt("kept", len(TEA), len(TEA + "\n" + COFFEE))
            await p.drain(client)
            service, store = deletion(p)
            service.accept("d", "synthetic-author", ("target",), None)
            await finish_deletion(p, client, service, store, "d")
            await assert_erased(p, client, marker)
            before = p.operation_ids()
            if replay == "receipt":
                response = p.http.post(
                    "/turns",
                    json=turn_body(
                        turn_id="target",
                        session_id=p.session,
                        actor=p.characters[0],
                        perceived_by=list(p.characters[:2]),
                        text=saved["text"],
                    ),
                )
                assert response.status_code == 409, response.text
                assert p.operation_ids() == before
                p.receipt("kept", len(TEA + "\n" + COFFEE), len(TEA + "\n" + COFFEE))
            else:
                p.clock.now += timedelta(seconds=601)
                p.service.confirm_expired_deliveries()
            assert p.operation_ids() - before, "Replay must enqueue projection"
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                modes = connection.execute(
                    "SELECT update_mode FROM outbox WHERE operation_id = ANY(%s)",
                    (list(p.operation_ids() - before),),
                ).fetchall()
            assert modes and all(
                row[0] == ("replace" if replay == "receipt" else "append") for row in modes
            )
            await p.drain(client)
            if replay == "reprocess":
                from digital_souls_memory_hindsight.evidence.projection import update_projection

                # Expiry first appends a previously undelivered turn. Reproject the
                # delivered scope through the existing ledger snapshot owner.
                before_rebuild = p.operation_ids()
                scope = p.service.get_delivery_scope("kept")
                assert scope is not None
                update_projection(
                    PostgresEvidenceStore(p.database), p.service.get_turn("kept"), scope
                )
                assert p.operation_ids() - before_rebuild
                await p.drain(client)
                for character in p.characters[:2]:
                    bank = bank_id(character)
                    await wait_idle(client, bank)
                    for identifier in await documents(client, bank):
                        reprocessed = await client.documents.reprocess_document(
                            bank_id=bank,
                            document_id=identifier,
                        )
                        assert reprocessed.success
                        deadline = asyncio.get_running_loop().time() + 60
                        while True:
                            status = await client.operations.get_operation_status(
                                bank_id=bank,
                                operation_id=reprocessed.operation_id,
                            )
                            if status.status == "completed":
                                break
                            assert status.status in {"pending", "processing"}, status.to_dict()
                            assert asyncio.get_running_loop().time() < deadline
                            await asyncio.sleep(0.1)
            await assert_erased(p, client, marker)

    asyncio.run(exercise())


@pytest.mark.parametrize("stop", ["ledger", "replace"])
def test_deletion_worker_restart_after_lease_expiry_rebuilds_only_once(
    projection: Projection,
    stop: str,
) -> None:
    p = projection
    marker = "deleted-" + uuid4().hex
    script = """
import asyncio, os, threading
from hindsight_client import Hindsight
from digital_souls_memory_hindsight.deletion.execution import DeletionExecutor
from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.hindsight_deletion import HindsightDeletion
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.worker import Worker
def pause():
    print('interrupted', flush=True)
    threading.Event().wait(90)
class Store(PostgresDeletionStore):
    def delete_turns(self, item, request, now):
        result = super().delete_turns(item, request, now)
        if os.environ['STOP'] == 'ledger':
            pause()
        return result
class Retain(HindsightRetain):
    async def execute(self, operation):
        result = await super().execute(operation)
        if operation.deletion_request_id and os.environ['STOP'] == 'replace':
            pause()
        return result
async def main():
    url = os.environ['DATABASE_URL']
    client = Hindsight(base_url=os.environ['HINDSIGHT_URL'], timeout=15.0)
    evidence = EvidenceService(PostgresEvidenceStore(url), frozenset())
    store = Store(url)
    service = DeletionService(store, evidence)
    worker = Worker(PostgresOutbox(url), Retain(client), evidence,
                    deletion=DeletionExecutor(service, store, HindsightDeletion(client)))
    try:
        while True:
            await worker.run_once()
            await asyncio.sleep(0.1)
    finally:
        await client.aclose()
asyncio.run(main())
"""

    async def exercise() -> None:
        async with hindsight_client() as client:
            await seed(p, client)
            target(p, marker)
            await p.drain(client)
            for character in p.characters[:2]:
                await wait_idle(client, bank_id(character))
            service, store = deletion(p)
            service.accept("d", "synthetic-author", ("target",), None)
            process = await asyncio.to_thread(
                subprocess.Popen,
                [sys.executable, "-u", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "DATABASE_URL": p.database, "STOP": stop},
            )
            try:
                assert process.stdout is not None
                ready, _, _ = await asyncio.to_thread(select.select, [process.stdout], [], [], 60)
                assert ready, "Worker did not reach the requested interruption point"
                assert process.stdout.readline().strip() == "interrupted"
            finally:
                process.kill()
                await asyncio.to_thread(process.communicate, timeout=10)
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                leases = connection.execute(
                    "SELECT lease_until FROM deletion_outbox WHERE request_id='d' "
                    "UNION ALL SELECT lease_until FROM outbox WHERE deletion_request_id='d'",
                ).fetchall()
                assert any(row[0] is not None for row in leases)
                p.clock.now = max(row[0] for row in leases if row[0] is not None) + timedelta(
                    seconds=1
                )
            await finish_deletion(p, client, service, store, "d")
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                rebuilt = connection.execute(
                    "SELECT bank_id, document_id, count(*) FROM outbox "
                    "WHERE deletion_request_id='d' AND fenced_operation_id IS NULL "
                    "GROUP BY bank_id, document_id",
                ).fetchall()
            assert len(rebuilt) == 2 and all(row[2] == 1 for row in rebuilt)
            for character in p.characters:
                text = await document_text(client, (bank_id(character), document_id(p.session)))
                assert text.count(TEA) == 1
            await assert_erased(p, client, marker)

    asyncio.run(exercise())


@pytest.mark.parametrize("valid_lease", [False, True])
@pytest.mark.parametrize("pause_after_ownership", [False, True])
@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("empty_document", [False, True])
def test_expired_append_claim_cannot_resurrect_a_deleted_turn(
    projection: Projection,
    valid_lease: bool,
    pause_after_ownership: bool,
    mode: str,
    empty_document: bool,
) -> None:
    p = projection
    marker = "deleted-" + uuid4().hex

    async def exercise() -> None:
        async with hindsight_client() as client:
            await seed(p, client)
            target_session = p.session
            if empty_document:
                target_session = "deleted-session-" + uuid4().hex
                response = p.http.post("/sessions", json=session_body(session_id=target_session))
                assert response.status_code == 201, response.text
            target_text = TEA + " 合成削除本文:" + marker
            if mode == "replace":
                p.post_turn("target", session_id=target_session, text=target_text)
                await p.drain(client)
                await wait_idle(client, bank_id(p.characters[0]))
            service, store = deletion(p)
            empty_claimed = Event()
            release_empty = Event()

            class EmptyQueue(PostgresOutbox):
                def claim(self, now: datetime, lease_for: timedelta) -> OutboxItem | None:
                    result = super().claim(now, lease_for)
                    assert result is None
                    empty_claimed.set()
                    assert release_empty.wait(30), "Deletion worker was not released"
                    return result

            # The deletion worker has already observed an empty retain queue when
            # the other worker claims the new append. Both use the real run_once.
            deleting = Worker(
                EmptyQueue(p.database),
                HindsightRetain(client),
                p.service,
                clock=p.clock,
                deletion=DeletionExecutor(service, store, HindsightDeletion(client), clock=p.clock),
            )
            deletion_task = asyncio.create_task(deleting.run_once())
            claimed = Event()
            release = Event()

            class Queue(PostgresOutbox):
                def claim(self, now: datetime, lease_for: timedelta) -> OutboxItem | None:
                    item = super().claim(now, lease_for)
                    assert item is not None and item.turn_id == "target"
                    if not pause_after_ownership:
                        claimed.set()
                        assert release.wait(STALE_CLAIM_RELEASE_TIMEOUT), (
                            "Stale claim was not released"
                        )
                    return item

            class Retain(HindsightRetain):
                async def execute(self, operation: RetainOperation) -> RetainResult:
                    if pause_after_ownership:
                        claimed.set()
                        assert await asyncio.to_thread(release.wait, STALE_CLAIM_RELEASE_TIMEOUT)
                    return await super().execute(operation)

            task: asyncio.Task[None] | None = None
            try:
                assert await asyncio.to_thread(empty_claimed.wait, 5)
                if mode == "append":
                    p.post_turn("target", session_id=target_session, text=target_text)
                else:
                    scope = p.service.get_delivery_scope("target")
                    assert scope is not None and scope.scope_id is not None
                    PostgresEvidenceStore(p.database).add_retain(
                        RetainOperation(
                            "target",
                            bank_id(p.characters[0]),
                            document_id(target_session),
                            target_text,
                            p.service.get_turn("target").occurred_at,
                            "replace",
                            source_scope_id=scope.scope_id,
                        )
                    )
                stale = Worker(Queue(p.database), Retain(client), p.service, clock=p.clock)
                task = asyncio.create_task(stale.run_once())
                assert await asyncio.to_thread(claimed.wait, 5)
                if not valid_lease:
                    p.clock.now += timedelta(seconds=61)
                service.accept("d", "synthetic-author", ("target",), None)
                release_empty.set()
                await asyncio.wait_for(deletion_task, timeout=10)
                if valid_lease:
                    assert service.get("d").status == "processing"
                    assert p.http.get("/turns/target").status_code == 200
                p.clock.now += timedelta(seconds=61)
                await finish_deletion(p, client, service, store, "d")
                with psycopg.connect(p.database, connect_timeout=5) as connection:
                    progress = connection.execute(
                        "SELECT progress FROM deletion_outbox WHERE request_id='d'"
                    ).fetchone()
                    assert progress is not None
                    fences = progress[0]["fenced_operations"]
                    rows = connection.execute(
                        "SELECT operation_id, fenced_operation_id, sequence_id FROM outbox "
                        "WHERE deletion_request_id='d' ORDER BY sequence_id"
                    ).fetchall()
                    assert len(fences) == sum(row[1] is not None for row in rows)
                    assert all(row[0] == row[1] for row in rows if row[1] is not None)
                    if not valid_lease:
                        assert len(fences) == 1
                    if fences:
                        assert rows[0][1] is not None
                        restored = PostgresOutbox(p.database).get(rows[0][0])
                        assert restored is not None
                        assert restored.operation.operation_id == rows[0][1]
                    for temporary in progress[0]["fenced_documents"]:
                        assert temporary["document_id"] not in await documents(
                            client, temporary["bank_id"]
                        )
            finally:
                release_empty.set()
                release.set()
                await asyncio.wait_for(deletion_task, timeout=10)
                if task is not None:
                    await asyncio.wait_for(task, timeout=30)
            await assert_erased(p, client, marker)

    asyncio.run(exercise())


def test_uncertain_append_delivered_before_fencing_is_removed_by_final_rebuild(
    projection: Projection,
) -> None:
    p = projection
    marker = "deleted-" + uuid4().hex

    async def exercise() -> None:
        async with hindsight_client() as client:
            await seed(p, client)
            p.post_turn("target", text=TEA + " 合成削除本文:" + marker)
            queue = PostgresOutbox(p.database)
            claimed = queue.claim(p.clock.now, timedelta(seconds=60))
            assert claimed is not None and claimed.turn_id == "target"
            await HindsightRetain(client).execute(claimed.operation)
            await wait_idle(client, claimed.bank_id)
            assert marker in await document_text(client, (claimed.bank_id, claimed.document_id))
            # Model an unknown transport result: the receiver completed, the ledger did not.
            assert queue.defer(claimed, p.clock.now, p.clock.now, "transport_error")
            service, store = deletion(p)
            service.accept("d", "synthetic-author", ("target",), None)
            item = store.claim(p.clock.now, timedelta(seconds=60))
            assert item is not None
            executor = DeletionExecutor(service, store, HindsightDeletion(client), clock=p.clock)
            assert await executor.step(item) == "waiting"
            assert store.defer(item, p.clock.now, p.clock.now, "pending")
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                fences = connection.execute(
                    "SELECT fenced_operation_id FROM outbox WHERE fenced_operation_id IS NOT NULL"
                ).fetchall()
            assert fences == [(claimed.operation.operation_id,)]
            await finish_deletion(p, client, service, store, "d")
            assert await document_text(client, (claimed.bank_id, claimed.document_id)) == TEA
            await assert_erased(p, client, marker)

    asyncio.run(exercise())
