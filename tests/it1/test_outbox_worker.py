"""Real ledger, leases and Hindsight, isolated from the application's live worker."""

import asyncio
import json
import os
import select
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from hindsight_client import Hindsight
from psycopg import sql

from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt, Session
from digital_souls_memory_hindsight.evidence.ports import EvidenceConflict, EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.outbox import (
    RetainOperation,
    RetainResult,
    RetainTarget,
    operation_id,
)
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.worker import Worker
from tests.it1.conftest import ResponseGate
from tests.ut.test_delivery_scope import CHARACTERS, Clock, draft

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("different_operation", [False, True], ids=["same-id", "different-id"])
def test_processing_operation_resend_preserves_append_identity(
    database: str, response_gate: ResponseGate, different_operation: bool
) -> None:
    bank = "it1-" + response_gate.marker
    clock = Clock(datetime.now(UTC) + timedelta(seconds=1))
    service = ledger(database, clock)
    content = f"架空の花子は識別子{response_gate.marker}のお茶が好きです。"
    saved = service.add_turn(draft(), retain_targets=(RetainTarget(bank, "session", content),))
    queue = PostgresOutbox(database)
    identifier = operation_id(saved.turn_id, bank, "session")

    async def exercise() -> None:
        client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
        identifiers = [identifier]

        async def status_of(value: str) -> str:
            status = await client.operations.get_operation_status(
                bank_id=bank, operation_id=value, _request_timeout=5.0
            )
            return str(status.status)

        async def wait_completed() -> None:
            deadline = asyncio.get_running_loop().time() + 60
            for value in identifiers:
                while (status := await status_of(value)) != "completed":
                    assert status in {"pending", "processing"}, status
                    assert asyncio.get_running_loop().time() < deadline, status
                    await asyncio.sleep(0.05)

        try:
            await client.acreate_bank(bank_id=bank)
            try:
                executor = HindsightRetain(client)
                worker = Worker(queue, executor, service, clock=clock)
                with patch.object(client, "aretain", wraps=client.aretain) as sent:
                    try:
                        await worker.run_once()
                        await response_gate.wait_entered()
                        before = await status_of(identifier)
                        assert before in {"pending", "processing"}, before
                        deferred = queue.get(identifier)
                        assert deferred is not None
                        assert (deferred.status, deferred.attempts, deferred.last_error) == (
                            "pending",
                            1,
                            "pending",
                        )
                        assert deferred.available_at == clock.now + timedelta(seconds=1)
                        clock.now = deferred.available_at
                        if different_operation:
                            other = replace(deferred.operation, turn_id="another-turn")
                            identifiers.append(other.operation_id)
                            assert (await executor.execute(other)).status == "pending"
                        else:
                            await worker.run_once()
                            again = queue.get(identifier)
                            assert again is not None
                            assert (again.status, again.attempts, again.last_error) == (
                                "pending",
                                2,
                                "pending",
                            )
                            assert again.operation == deferred.operation
                        after = await status_of(identifier)
                        assert after in {"pending", "processing"}, after
                        assert sent.await_count == 2
                        first = sent.await_args_list[0].kwargs
                        second = sent.await_args_list[1].kwargs
                        assert first == {
                            "bank_id": bank,
                            "document_id": "session",
                            "content": content,
                            "timestamp": saved.occurred_at,
                            "update_mode": "append",
                            "retain_async": True,
                            "operation_id": identifier,
                        }
                        assert second == {**first, "operation_id": identifiers[-1]}
                        assert (
                            first["operation_id"] != second["operation_id"]
                        ) == different_operation
                    finally:
                        response_gate.release()
                    await wait_completed()
                    await response_gate.wait_finished()
                    clock.now += timedelta(seconds=1)
                    await worker.run_once()
                    finished = queue.get(identifier)
                    assert finished is not None and finished.status == "completed"
                    document = await client.documents.get_document(
                        bank_id=bank, document_id="session", _request_timeout=5.0
                    )
                    assert document.original_text is not None
                    count = document.original_text.count(content)
                    assert count == (2 if different_operation else 1), document.original_text
                    print(
                        json.dumps(
                            {
                                "case": "different-id" if different_operation else "same-id",
                                "before": before,
                                "after": after,
                                "operation_ids": identifiers,
                                "timestamp": saved.occurred_at.isoformat(),
                                "content_count": count,
                                "outbox": finished.status,
                            }
                        )
                    )
            finally:
                response_gate.release()
                try:
                    await wait_completed()
                finally:
                    await response_gate.wait_finished()
                    await client.banks.delete_bank(bank_id=bank, _request_timeout=10.0)
        finally:
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise())


def ledger(url: str, clock: Clock) -> EvidenceService:
    service = EvidenceService(PostgresEvidenceStore(url), CHARACTERS, clock=clock)
    service.add_session(Session("s-1", clock.now - timedelta(days=2), None, "Web", True))
    return service


def test_turn_and_outbox_commit_together_and_retry_cannot_change_payload(database: str) -> None:
    clock = Clock(datetime.now(UTC))
    service = ledger(database, clock)
    target = RetainTarget("bank-a", "session", "架空のお茶")
    saved = service.add_turn(draft(), retain_targets=(target,))
    service.add_turn(draft(), retain_targets=(target,))
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM turns").fetchone() == (1,)
        assert connection.execute("SELECT content, occurred_at FROM outbox").fetchall() == [
            (target.content, saved.occurred_at)
        ]
    with pytest.raises(EvidenceConflict):
        service.add_turn(draft(), retain_targets=(replace(target, content="異なる本文"),))
    assert PostgresOutbox(database).get(operation_id("t-1", "bank-a", "session")) is not None


def test_outbox_insert_failure_rolls_back_real_turn_and_delivery(database: str) -> None:
    service = ledger(database, Clock(datetime.now(UTC)))
    with psycopg.connect(database, connect_timeout=5) as connection:
        connection.execute("""
            CREATE FUNCTION reject_outbox_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic failure'; END; $$;
            CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox
                FOR EACH STATEMENT EXECUTE FUNCTION reject_outbox_insert();
        """)
    target = RetainTarget("bank", "session", "架空のお茶")
    with pytest.raises(EvidenceUnavailable):
        service.add_turn(draft(), retain_targets=(target,))
    with psycopg.connect(database, connect_timeout=5) as connection:
        for table in ("turns", "delivery_scopes", "outbox"):
            assert connection.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone() == (0,)
        connection.execute("DROP TRIGGER reject_outbox ON outbox")
    service.add_turn(draft(), retain_targets=(target,))
    assert PostgresOutbox(database).get(operation_id("t-1", "bank", "session")) is not None


def test_claim_is_exclusive_and_expired_owner_cannot_finish_new_lease(database: str) -> None:
    now = datetime.now(UTC)
    service = ledger(database, Clock(now))
    target = RetainTarget("bank", "session", "架空のお茶")
    service.add_turn(draft(), retain_targets=(target,))
    barrier = Barrier(2)

    def claim() -> object:
        barrier.wait(timeout=5)
        return PostgresOutbox(database).claim(now + timedelta(seconds=1), timedelta(seconds=10))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert sum(value is not None for value in claims) == 1
    queue = PostgresOutbox(database)
    old = queue.get(operation_id("t-1", "bank", "session"))
    assert old is not None and old.attempts == 1
    assert queue.claim(now + timedelta(seconds=10), timedelta(seconds=10)) is None
    renewed = queue.claim(now + timedelta(seconds=11), timedelta(seconds=10))
    assert renewed is not None and renewed.attempts == 2
    assert renewed.operation == old.operation
    assert renewed.lease_token != old.lease_token
    assert not queue.complete(old, now + timedelta(seconds=12))
    assert not queue.defer(old, now + timedelta(seconds=12), now, "transport_error")
    assert not queue.fail(old, now + timedelta(seconds=12), "retain_failed")
    assert queue.complete(renewed, now + timedelta(seconds=12))
    assert queue.claim(now + timedelta(days=1), timedelta(seconds=10)) is None


def test_worker_persists_transport_failure_then_retries_same_operation(database: str) -> None:
    clock = Clock(datetime.now(UTC) + timedelta(seconds=1))
    service = ledger(database, clock)
    service.add_turn(draft(), retain_targets=(RetainTarget("bank", "session", "架空のお茶"),))
    queue = PostgresOutbox(database)

    class UnavailableExecutor:
        async def execute(self, operation: RetainOperation) -> RetainResult:
            raise TimeoutError("synthetic private detail")

    asyncio.run(Worker(queue, UnavailableExecutor(), service, clock=clock).run_once())
    saved = PostgresOutbox(database).get(operation_id("t-1", "bank", "session"))
    assert saved is not None
    assert (saved.status, saved.attempts, saved.last_error) == ("pending", 1, "transport_error")
    assert saved.available_at == clock.now + timedelta(seconds=1)
    assert queue.claim(clock.now, timedelta(seconds=60)) is None
    clock.now += timedelta(seconds=1)
    retried = queue.claim(clock.now, timedelta(seconds=60))
    assert retried is not None and retried.attempts == 2
    assert retried.operation == saved.operation


def test_same_worker_persists_expiry_and_preserves_receipt_range(database: str) -> None:
    clock = Clock(datetime.now(UTC))
    service = ledger(database, clock)
    saved = service.add_turn(draft())
    service.add_turn(replace(draft(), turn_id="received"))
    service.add_receipt(DeliveryReceipt("received", "partial", 2, "Web", clock.now))
    received = service.get_delivery_scope("received")
    worker = Worker(PostgresOutbox(database), None, service, clock=clock)
    reader = EvidenceService(PostgresEvidenceStore(database), CHARACTERS)

    async def exercise() -> None:
        clock.now += timedelta(seconds=599)
        await worker.run_once()
        assert reader.get_projection_text(saved.turn_id, "char-b") is None
        clock.now += timedelta(seconds=1)
        await worker.run_once()
        scope = reader.get_delivery_scope(saved.turn_id)
        assert scope is not None and scope.status == "assumed_confirmed"
        assert reader.get_projection_text(saved.turn_id, "char-b") == saved.text
        assert reader.get_delivery_scope("received") == received

    asyncio.run(exercise())


def test_outbox_projects_same_turn_to_each_explicit_bank_once(database: str) -> None:
    token = uuid4().hex
    banks = (f"it1-{token}-a", f"it1-{token}-b")
    clock = Clock(datetime.now(UTC) + timedelta(seconds=1))
    service = ledger(database, clock)
    content = f"架空の花子は識別子{token}のお茶が好きです。"
    targets = tuple(RetainTarget(bank, "session", content) for bank in banks)
    saved = service.add_turn(draft(), retain_targets=targets)
    assert service.add_turn(draft(), retain_targets=targets) == saved

    async def exercise() -> None:
        client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
        created: list[str] = []
        try:
            try:
                for bank in banks:
                    await client.acreate_bank(bank_id=bank)
                    created.append(bank)
                queue = PostgresOutbox(database)
                worker = Worker(queue, HindsightRetain(client), service, clock=clock)
                deadline = asyncio.get_running_loop().time() + 60
                while True:
                    await worker.run_once()
                    items = [
                        queue.get(operation_id(saved.turn_id, bank, "session")) for bank in banks
                    ]
                    assert all(item is not None and item.status != "failed" for item in items)
                    if all(item is not None and item.status == "completed" for item in items):
                        break
                    assert asyncio.get_running_loop().time() < deadline
                    clock.now += timedelta(seconds=1)
                    await asyncio.sleep(0.1)
                for bank in banks:
                    document = await client.documents.get_document(
                        bank_id=bank,
                        document_id="session",
                        _request_timeout=5.0,
                    )
                    assert document.original_text is not None
                    assert document.original_text.count(content) == 1
                assert queue.claim(clock.now, timedelta(seconds=60)) is None
            finally:
                for bank in created:
                    await client.banks.delete_bank(bank_id=bank, _request_timeout=10.0)
        finally:
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise())


def test_application_lifespan_drains_outbox_in_same_process(database: str) -> None:
    token = uuid4().hex
    bank = "it1-" + token
    content = f"架空の花子は識別子{token}のお茶が好きです。"
    script = """
import os, time
from datetime import UTC, datetime, timedelta
from fastapi.testclient import TestClient
from digital_souls_memory_hindsight.app import app, ledger_service
from digital_souls_memory_hindsight.evidence.domain import Session
from digital_souls_memory_hindsight.outbox import RetainTarget, operation_id
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from tests.ut.test_delivery_scope import draft
with TestClient(app):
    started = datetime.now(UTC)-timedelta(days=2)
    ledger_service.add_session(Session('s-1', started, None, 'Web', True))
    target = RetainTarget(os.environ['TARGET_BANK'], 'session', os.environ['TARGET_CONTENT'])
    saved = ledger_service.add_turn(draft(), retain_targets=(target,))
    queue = PostgresOutbox(os.environ['DATABASE_URL'])
    deadline = time.monotonic() + 60
    while True:
        item = queue.get(operation_id(saved.turn_id, target.bank_id, target.document_id))
        assert item is not None and item.status != 'failed'
        if item.status == 'completed':
            break
        assert time.monotonic() < deadline
        time.sleep(0.1)
"""

    async def exercise() -> None:
        client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
        try:
            await client.acreate_bank(bank_id=bank)
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-c", script],
                    env={
                        **os.environ,
                        "DATABASE_URL": database,
                        "EVIDENCE_CHARACTER_IDS": '["char-a","char-b"]',
                        "TARGET_BANK": bank,
                        "TARGET_CONTENT": content,
                    },
                    capture_output=True,
                    text=True,
                    timeout=75,
                    check=False,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                document = await client.documents.get_document(
                    bank_id=bank,
                    document_id="session",
                    _request_timeout=5.0,
                )
                assert document.original_text is not None
                assert document.original_text.count(content) == 1
            finally:
                await client.banks.delete_bank(bank_id=bank, _request_timeout=10.0)
        finally:
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise())


@pytest.mark.parametrize("stop_after_retain", [False, True])
def test_worker_process_restart_recovers_saved_operation_without_duplicate_append(
    database: str,
    stop_after_retain: bool,
) -> None:
    token = uuid4().hex
    bank = "it1-" + token
    now = datetime.now(UTC)
    clock = Clock(now + timedelta(seconds=1))
    service = ledger(database, clock)
    content = f"架空の花子は識別子{token}のお茶が好きです。"
    target = RetainTarget(bank, "session", content)
    saved = service.add_turn(draft(), retain_targets=(target,))
    identifier = operation_id(saved.turn_id, bank, "session")
    script = """
import asyncio, os, threading
from datetime import UTC, datetime
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.worker import Worker
from hindsight_client import Hindsight
class Queue(PostgresOutbox):
    def claim(self, now, lease_for):
        result = super().claim(now, lease_for)
        if result is not None and os.environ['STOP_AFTER_RETAIN'] == 'False':
            print('claimed', flush=True)
            threading.Event().wait(90)
        return result
    def complete(self, item, now):
        print('retained', flush=True)
        threading.Event().wait(90)
        return super().complete(item, now)
async def main():
    client = Hindsight(base_url=os.environ['HINDSIGHT_URL'], timeout=10.0)
    service = EvidenceService(PostgresEvidenceStore(os.environ['DATABASE_URL']), frozenset())
    worker = Worker(Queue(os.environ['DATABASE_URL']), HindsightRetain(client), service)
    try:
        while True:
            await worker.run_once()
            await asyncio.sleep(0.1)
    finally:
        await client.aclose()
asyncio.run(main())
"""

    async def exercise() -> None:
        client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
        try:
            await client.acreate_bank(bank_id=bank)
            try:
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    [sys.executable, "-u", "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={
                        **os.environ,
                        "DATABASE_URL": database,
                        "STOP_AFTER_RETAIN": str(stop_after_retain),
                    },
                )
                try:
                    assert process.stdout is not None
                    ready, _, _ = await asyncio.to_thread(
                        select.select, [process.stdout], [], [], 60
                    )
                    assert ready, "Worker did not reach the requested interruption point"
                    assert process.stdout.readline().strip() == (
                        "retained" if stop_after_retain else "claimed"
                    )
                finally:
                    process.kill()
                    await asyncio.to_thread(process.communicate, timeout=10)
                queue = PostgresOutbox(database)
                interrupted = queue.get(identifier)
                assert interrupted is not None and interrupted.status == "processing"
                assert interrupted.lease_until is not None
                clock.now = interrupted.lease_until + timedelta(seconds=1)
                worker = Worker(queue, HindsightRetain(client), service, clock=clock)
                deadline = asyncio.get_running_loop().time() + 60
                while True:
                    await worker.run_once()
                    current = queue.get(identifier)
                    assert current is not None and current.status != "failed"
                    if current.status == "completed":
                        break
                    assert asyncio.get_running_loop().time() < deadline
                    clock.now += timedelta(seconds=1)
                    await asyncio.sleep(0.25)
                document = await client.documents.get_document(
                    bank_id=bank,
                    document_id="session",
                    _request_timeout=5.0,
                )
                assert document.original_text is not None
                assert document.original_text.count(content) == 1
                assert current.operation.occurred_at == saved.occurred_at
                assert current.attempts > interrupted.attempts
                assert queue.claim(clock.now, timedelta(seconds=60)) is None
            finally:
                await client.banks.delete_bank(bank_id=bank, _request_timeout=10.0)
        finally:
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise())
