"""Automatic HTTP-to-outbox projection and isolation against the pinned server."""

import asyncio
import os
import re
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import NotFoundException

from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.worker import Worker
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import receipt_body, session_body, turn_body

pytestmark = pytest.mark.it1
TEA = "架空の花子は翡翠茶を飲みます。"
SECRET = "架空の金庫の暗証番号は七九三です。"
COFFEE = "架空の太郎は琥珀珈琲を飲みます。"
MOON = "架空の月には青銅塔があります。"


@dataclass
class Projection:
    database: str
    service: EvidenceService
    http: TestClient
    clock: Clock
    characters: tuple[str, str, str]
    session: str

    def post_turn(self, turn_id: str, **changes: object) -> dict[str, object]:
        response = self.http.post(
            "/turns",
            json=turn_body(
                **{
                    "turn_id": turn_id,
                    "session_id": self.session,
                    "actor": self.characters[0],
                    "perceived_by": [self.characters[0]],
                    **changes,
                }
            ),
        )
        assert response.status_code == 201, response.text
        return cast(dict[str, object], response.json())

    def receipt(self, turn_id: str, limit: int, total: int) -> None:
        status = "none" if limit == 0 else "full" if limit == total else "partial"
        response = self.http.post(
            f"/turns/{turn_id}/receipts",
            json=receipt_body(status=status, spoken_until=limit),
        )
        assert response.status_code == 201, response.text

    def target(self, turn_id: str) -> tuple[str, str]:
        with psycopg.connect(self.database, connect_timeout=5) as connection:
            rows = connection.execute(
                "SELECT DISTINCT bank_id, document_id FROM outbox WHERE turn_id = %s",
                (turn_id,),
            ).fetchall()
        assert len(rows) == 1, rows
        return str(rows[0][0]), str(rows[0][1])

    def operation_ids(self) -> set[str]:
        with psycopg.connect(self.database, connect_timeout=5) as connection:
            return {str(row[0]) for row in connection.execute("SELECT operation_id FROM outbox")}

    async def drain(self, client: Hindsight) -> None:
        worker = Worker(
            PostgresOutbox(self.database), HindsightRetain(client), self.service, clock=self.clock
        )
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            await worker.run_once()
            with psycopg.connect(self.database, connect_timeout=5) as connection:
                statuses = [row[0] for row in connection.execute("SELECT status FROM outbox")]
            assert statuses, "Automatic registration did not enqueue projection"
            assert "failed" not in statuses, statuses
            if all(status == "completed" for status in statuses):
                return
            assert asyncio.get_running_loop().time() < deadline, statuses
            self.clock.now += timedelta(seconds=1)
            await asyncio.sleep(0.1)


@pytest.fixture
def projection(database: str) -> Iterator[Projection]:
    token = uuid4().hex
    characters = (f"char-a-{token}", f"char_a-{token}", f"char-c-{token}")
    clock = Clock(datetime.now(UTC) + timedelta(seconds=1))
    service = EvidenceService(PostgresEvidenceStore(database), frozenset(characters), clock=clock)
    app = FastAPI()
    app.include_router(evidence_router(lambda: service))
    with TestClient(app) as http:
        session = "s-1-" + token
        response = http.post("/sessions", json=session_body(session_id=session))
        assert response.status_code == 201
        value = Projection(database, service, http, clock, characters, session)
        try:
            yield value
        finally:

            async def cleanup() -> None:
                client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
                try:
                    with psycopg.connect(database, connect_timeout=5) as connection:
                        banks = [
                            str(row[0])
                            for row in connection.execute("SELECT DISTINCT bank_id FROM outbox")
                        ]
                    for bank in banks:
                        # Registration can fail before the worker creates the bank.
                        existing = await client.banks.list_banks(_request_timeout=5.0)
                        if any(item.bank_id == bank for item in existing.banks):
                            await wait_idle(client, bank)
                            await client.banks.delete_bank(bank_id=bank, _request_timeout=10.0)
                finally:
                    await client.aclose()  # type: ignore[no-untyped-call]

            asyncio.run(cleanup())


@asynccontextmanager
async def hindsight_client() -> AsyncIterator[Hindsight]:
    client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
    try:
        yield client
    finally:
        await client.aclose()  # type: ignore[no-untyped-call]


async def wait_idle(client: Hindsight, bank: str) -> None:
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        operations = await client.operations.list_operations(bank_id=bank, _request_timeout=5.0)
        assert all(item.status != "failed" for item in operations.operations), operations.to_dict()
        if all(item.status not in {"pending", "processing"} for item in operations.operations):
            return
        assert asyncio.get_running_loop().time() < deadline, operations.to_dict()
        await asyncio.sleep(0.1)


async def document_text(client: Hindsight, target: tuple[str, str]) -> str:
    document = await client.documents.get_document(
        bank_id=target[0], document_id=target[1], _request_timeout=5.0
    )
    assert document.original_text is not None
    return str(document.original_text)


def text_fields(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_fields(item)


def assert_no_private_tokens(value: object, forbidden: tuple[str, ...]) -> None:
    for text in text_fields(value):
        normalized = re.sub(r"\s+", "", text).casefold()
        for token in forbidden:
            assert re.sub(r"\s+", "", token).casefold() not in normalized, text


async def assert_search_isolated(
    client: Hindsight, target: tuple[str, str], allowed: str, forbidden: tuple[str, ...]
) -> None:
    bank, _ = target
    await wait_idle(client, bank)
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        operations = await client.operations.list_operations(
            bank_id=bank, type="consolidation", _request_timeout=5.0
        )
        assert all(item.status not in {"failed", "cancelled"} for item in operations.operations)
        if operations.operations and all(
            item.status == "completed" for item in operations.operations
        ):
            break
        assert asyncio.get_running_loop().time() < deadline, operations.to_dict()
        await asyncio.sleep(0.1)
    original = await document_text(client, target)
    assert allowed in original
    assert_no_private_tokens(original, forbidden)
    recalled = await client.arecall(
        bank_id=bank,
        query="架空の花子、太郎、お茶、珈琲、金庫、月について知っていること",
        types=["world", "experience", "observation"],
        include_chunks=True,
        include_source_facts=True,
        max_tokens=10000,
    )
    facts = [item for item in recalled.results if item.type in {"world", "experience"}]
    observations = [item for item in recalled.results if item.type == "observation"]
    assert any(allowed in item.text for item in facts), recalled.to_dict()
    assert any(allowed in item.text for item in observations), recalled.to_dict()
    assert recalled.chunks and any(allowed in item.text for item in recalled.chunks.values())
    assert_no_private_tokens(recalled.to_dict(), forbidden)
    reflected = await client.areflect(
        bank_id=bank,
        query="知っている架空の事実をすべて原文とともに答えてください。",
        include_facts=True,
        include_tool_calls=True,
    )
    assert allowed in reflected.text, reflected.to_dict()
    assert reflected.based_on is not None and reflected.based_on.memories
    assert reflected.trace is not None and reflected.trace.tool_calls
    assert_no_private_tokens(reflected.to_dict(), forbidden)


def test_same_character_uses_one_bank_and_distinct_session_documents(
    projection: Projection,
) -> None:
    p = projection
    p.post_turn("first", text=TEA)
    first = p.target("first")
    other_session = p.session.replace("s-1-", "s_1-", 1)
    assert p.http.post("/sessions", json=session_body(session_id=other_session)).status_code == 201
    p.post_turn("other-session", session_id=other_session, text=COFFEE)
    second = p.target("other-session")
    assert first[0] == second[0]
    assert first[1] != second[1]

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            assert (await document_text(client, first)).strip() == TEA
            assert (await document_text(client, second)).strip() == COFFEE

    asyncio.run(exercise())


def test_similar_character_ids_do_not_share_a_bank(projection: Projection) -> None:
    p = projection
    p.post_turn("first", text=TEA)
    p.post_turn("second", actor=p.characters[1], perceived_by=[p.characters[1]], text=COFFEE)
    first, second = p.target("first"), p.target("second")
    assert first[0] != second[0]

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            assert (await document_text(client, first)).strip() == TEA
            assert (await document_text(client, second)).strip() == COFFEE

    asyncio.run(exercise())


def test_same_session_appends_distinct_turns_and_retries_without_duplicate_text(
    projection: Projection,
) -> None:
    p = projection
    p.post_turn("first", text=TEA)
    first = p.target("first")

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            assert (await document_text(client, first)).count(TEA) == 1
            p.post_turn("first", text=TEA)
            await p.drain(client)
            assert (await document_text(client, first)).count(TEA) == 1
            p.post_turn("second", text=COFFEE)
            assert p.target("second") == first
            await p.drain(client)
            combined = await document_text(client, first)
            assert combined.count(TEA) == combined.count(COFFEE) == 1
            assert combined.index(TEA) < combined.index(COFFEE)
            p.post_turn("third", text=TEA)
            assert p.target("third") == first
            await p.drain(client)
            final = await document_text(client, first)
            assert final.count(TEA) == 2 and final.count(COFFEE) == 1

    asyncio.run(exercise())


def test_three_character_banks_isolate_text_facts_observations_chunks_and_reflect(
    projection: Projection,
) -> None:
    p = projection
    a, b, c = p.characters
    p.post_turn("a-seed", text=TEA, kind="thought")
    p.post_turn("b-seed", text=COFFEE, kind="thought", actor=b, perceived_by=[b])
    p.post_turn("c-seed", text=MOON, kind="thought", actor=c, perceived_by=[c])
    targets = [p.target(turn) for turn in ("a-seed", "b-seed", "c-seed")]
    p.post_turn("spoken", text=TEA + "\n" + SECRET, perceived_by=[a, c])
    p.receipt("spoken", len(TEA), len(TEA + "\n" + SECRET))
    before = p.operation_ids()
    p.post_turn("unperceived", actor="user", perceived_by=[], text="架空の紅玉島。")
    assert p.operation_ids() == before

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            await assert_search_isolated(
                client, targets[0], "翡翠茶", ("琥珀珈琲", "青銅塔", "紅玉島")
            )
            await assert_search_isolated(
                client, targets[1], "琥珀珈琲", ("翡翠茶", "金庫", "七九三", "青銅塔", "紅玉島")
            )
            await assert_search_isolated(
                client, targets[2], "翡翠茶", ("琥珀珈琲", "金庫", "七九三", "紅玉島")
            )

    asyncio.run(exercise())


def test_late_receipts_rebuild_same_document_through_shrink_expand_shrink_and_empty(
    projection: Projection,
) -> None:
    p = projection
    a, _, c = p.characters
    p.post_turn("speaker", text=MOON, kind="thought")
    speaker = p.target("speaker")
    p.post_turn("listener", text=MOON, kind="thought", actor=c, perceived_by=[c])
    listener = p.target("listener")
    other_session = p.session + "-other"
    assert p.http.post("/sessions", json=session_body(session_id=other_session)).status_code == 201
    p.post_turn(
        "other-session",
        actor=c,
        perceived_by=[c],
        session_id=other_session,
        text=COFFEE,
        kind="thought",
    )
    other = p.target("other-session")
    assert other[0] == listener[0] and other[1] != listener[1]
    full = TEA + "\n" + SECRET
    saved = p.post_turn("spoken", text=full, perceived_by=[a, c])
    p.clock.now += timedelta(seconds=600)
    p.service.confirm_expired_deliveries()

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            await assert_search_isolated(client, listener, "七九三", ())
            for limit in (len(TEA), len(full), len(TEA), 0):
                before = p.operation_ids()
                p.receipt("spoken", limit, len(full))
                after = p.operation_ids()
                assert len(after - before) == 1
                with patch.object(client, "aretain", wraps=client.aretain) as sent:
                    await p.drain(client)
                    assert sent.await_args_list
                    assert all(
                        call.kwargs["update_mode"] == "replace"
                        and call.kwargs["bank_id"] == listener[0]
                        and call.kwargs["document_id"] == listener[1]
                        for call in sent.await_args_list
                    )
                assert p.target("listener") == listener
                text = await document_text(client, listener)
                assert MOON in text
                if limit == len(full):
                    await assert_search_isolated(client, listener, "七九三", ())
                else:
                    await assert_search_isolated(
                        client,
                        listener,
                        "翡翠茶" if limit else "青銅塔",
                        ("金庫", "七九三") if limit else ("翡翠茶", "金庫", "七九三"),
                    )
                assert full in await document_text(client, speaker)
                assert (await document_text(client, other)).strip() == COFFEE
                assert p.http.get("/turns/spoken").json() == saved
                p.receipt("spoken", limit, len(full))
                assert p.operation_ids() == after

    asyncio.run(exercise())


@pytest.mark.parametrize("finish", ["retry", "failed", "lease-expired"])
def test_same_document_keeps_enqueue_order_across_defer_failure_and_lease_recovery(
    projection: Projection, finish: str
) -> None:
    p = projection
    p.post_turn("first", text=TEA)
    p.post_turn("second", text=COFFEE)
    queue = PostgresOutbox(p.database)
    now = p.clock.now + timedelta(seconds=1)
    first = queue.claim(now, timedelta(seconds=10))
    assert first is not None and first.turn_id == "first"
    assert queue.claim(now, timedelta(seconds=10)) is None
    if finish == "failed":
        assert queue.fail(first, now, "retain_failed")
        assert queue.claim(now + timedelta(days=1), timedelta(seconds=10)) is None
        return
    if finish == "retry":
        assert queue.defer(first, now, now + timedelta(seconds=30), "transport_error")
        assert queue.claim(now + timedelta(seconds=1), timedelta(seconds=10)) is None
        now += timedelta(seconds=30)
    else:
        now += timedelta(seconds=10)
    retried = queue.claim(now, timedelta(seconds=10))
    assert retried is not None and retried.operation == first.operation
    assert queue.complete(retried, now)
    second = queue.claim(now, timedelta(seconds=10))
    assert second is not None and second.turn_id == "second"


def test_delivery_confirmation_order_wins_over_event_time(projection: Projection) -> None:
    p = projection
    a, _, c = p.characters
    p.post_turn("listener", actor=c, perceived_by=[c], text=MOON, kind="thought")
    listener = p.target("listener")
    p.post_turn("older", text=TEA, perceived_by=[a, c], occurred_at="2020-01-01T00:00:00Z")
    p.post_turn("newer", text=COFFEE, perceived_by=[a, c], occurred_at="2025-01-01T00:00:00Z")
    p.receipt("newer", len(COFFEE), len(COFFEE))
    p.receipt("older", len(TEA), len(TEA))

    async def exercise() -> None:
        async with hindsight_client() as client:
            with patch.object(client, "aretain", wraps=client.aretain) as sent:
                await p.drain(client)
            text = await document_text(client, listener)
            assert text.index(COFFEE) < text.index(TEA)
            calls = [
                call.kwargs
                for call in sent.await_args_list
                if call.kwargs["bank_id"] == listener[0]
            ]
            times = {call["content"]: call["timestamp"] for call in calls}
            assert times[TEA] == datetime(2020, 1, 1, tzinfo=UTC)
            assert times[COFFEE] == datetime(2025, 1, 1, tzinfo=UTC)

    asyncio.run(exercise())


def test_projection_insert_failure_rolls_back_real_http_registration(
    projection: Projection,
) -> None:
    p = projection
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        connection.execute("""
            CREATE FUNCTION reject_projection_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic failure'; END; $$;
            CREATE TRIGGER reject_projection BEFORE INSERT ON outbox
                FOR EACH STATEMENT EXECUTE FUNCTION reject_projection_insert();
        """)
    body = turn_body(session_id=p.session, actor=p.characters[0], perceived_by=[p.characters[0]])
    response = p.http.post("/turns", json=body)
    assert response.status_code == 503
    assert p.http.get("/turns/t-1").status_code == 404
    assert p.operation_ids() == set()
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM delivery_scopes").fetchone() == (0,)
        connection.execute("DROP TRIGGER reject_projection ON outbox")
    assert p.http.post("/turns", json=body).status_code == 201
    assert len(p.operation_ids()) == 1


def test_replace_snapshot_and_identity_survive_retry_and_return_to_previous_range(
    projection: Projection,
) -> None:
    p = projection
    a, _, c = p.characters
    p.post_turn("speaker", text=MOON, kind="thought")
    speaker_bank, _ = p.target("speaker")
    full = TEA + "\n" + SECRET
    p.post_turn("spoken", text=full, perceived_by=[a, c])
    p.clock.now += timedelta(seconds=600)
    p.service.confirm_expired_deliveries()
    initial = p.operation_ids()
    p.receipt("spoken", len(TEA), len(full))
    first_replace_ids = p.operation_ids() - initial
    assert len(first_replace_ids) == 1
    queue = PostgresOutbox(p.database)
    # Complete the saved initial appends without involving an external server.
    while True:
        item = queue.claim(p.clock.now, timedelta(seconds=60))
        assert item is not None
        if item.operation.operation_id in first_replace_ids:
            break
        assert queue.complete(item, p.clock.now)
    first = item
    before_retry = p.operation_ids()
    PostgresEvidenceStore(p.database).add_retain(first.operation)
    assert p.operation_ids() == before_retry
    assert first.bank_id != speaker_bank
    assert first.content == TEA
    assert queue.defer(first, p.clock.now, p.clock.now + timedelta(seconds=30), "transport_error")
    p.receipt("spoken", len(full), len(full))
    p.receipt("spoken", len(TEA), len(full))
    assert len(p.operation_ids() - initial) == 3
    assert queue.claim(p.clock.now, timedelta(seconds=60)) is None
    p.clock.now += timedelta(seconds=30)
    retried = queue.claim(p.clock.now, timedelta(seconds=60))
    assert retried is not None and retried.operation == first.operation
    assert queue.complete(retried, p.clock.now)
    expanded = queue.claim(p.clock.now, timedelta(seconds=60))
    assert expanded is not None and expanded.content == full
    assert queue.complete(expanded, p.clock.now)
    shrunk = queue.claim(p.clock.now, timedelta(seconds=60))
    assert shrunk is not None and shrunk.content == TEA
    assert shrunk.operation.operation_id != first.operation.operation_id
    assert shrunk.document_id == first.document_id and shrunk.bank_id == first.bank_id
    assert queue.complete(shrunk, p.clock.now)
    p.receipt("spoken", 0, len(full))
    deleted = queue.claim(p.clock.now, timedelta(seconds=60))
    assert deleted is not None and deleted.update_mode == "delete"
    assert deleted.content == ""
    assert (deleted.bank_id, deleted.document_id) == (first.bank_id, first.document_id)
    before_retry = p.operation_ids()
    PostgresEvidenceStore(p.database).add_retain(deleted.operation)
    assert p.operation_ids() == before_retry
    assert queue.defer(deleted, p.clock.now, p.clock.now + timedelta(seconds=30), "transport_error")
    p.receipt("spoken", len(full), len(full))
    p.post_turn("after-delete", actor=c, perceived_by=[c], text=MOON, kind="thought")
    assert queue.claim(p.clock.now, timedelta(seconds=60)) is None
    p.clock.now += timedelta(seconds=30)
    retried_delete = queue.claim(p.clock.now, timedelta(seconds=60))
    assert retried_delete is not None and retried_delete.operation == deleted.operation
    assert queue.complete(retried_delete, p.clock.now)
    restored = queue.claim(p.clock.now, timedelta(seconds=60))
    assert restored is not None and restored.update_mode == "replace" and restored.content == full
    assert queue.complete(restored, p.clock.now)
    appended = queue.claim(p.clock.now, timedelta(seconds=60))
    assert appended is not None and appended.update_mode == "append" and appended.content == MOON
    assert (appended.bank_id, appended.document_id) == (deleted.bank_id, deleted.document_id)


def test_none_receipt_deletes_document_then_expansion_restores_same_document(
    projection: Projection,
) -> None:
    p = projection
    a, _, c = p.characters
    p.post_turn("speaker", text=MOON, kind="thought")
    speaker_bank, _ = p.target("speaker")
    full = TEA + "\n" + SECRET
    p.post_turn("spoken", text=full, perceived_by=[a, c])
    p.clock.now += timedelta(seconds=600)
    p.service.confirm_expired_deliveries()
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT bank_id, document_id FROM outbox WHERE turn_id = %s AND bank_id != %s",
            ("spoken", speaker_bank),
        ).fetchall()
    assert len(rows) == 1
    listener = str(rows[0][0]), str(rows[0][1])

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            await assert_search_isolated(client, listener, "翡翠茶", ())
            p.receipt("spoken", 0, len(full))
            with (
                patch.object(client, "aretain", wraps=client.aretain) as sent,
                patch.object(
                    client.documents, "delete_document", wraps=client.documents.delete_document
                ) as deleted,
            ):
                await p.drain(client)
            sent.assert_not_awaited()
            deleted.assert_awaited_once()
            assert deleted.await_args is not None
            assert deleted.await_args.kwargs["bank_id"] == listener[0]
            assert deleted.await_args.kwargs["document_id"] == listener[1]
            await wait_idle(client, listener[0])
            with pytest.raises(NotFoundException):
                await document_text(client, listener)
            recalled = await client.arecall(
                bank_id=listener[0],
                query="翡翠茶 金庫",
                include_chunks=True,
                include_source_facts=True,
                types=["world", "experience", "observation"],
            )
            assert_no_private_tokens(recalled.to_dict(), ("翡翠茶", "金庫", "七九三"))
            reflected = await client.areflect(
                bank_id=listener[0],
                query="知っている架空の事実を答えてください。",
                include_facts=True,
                include_tool_calls=True,
            )
            assert_no_private_tokens(reflected.to_dict(), ("翡翠茶", "金庫", "七九三"))
            before = p.operation_ids()
            p.receipt("spoken", 0, len(full))
            assert p.operation_ids() == before
            p.receipt("spoken", len(TEA), len(full))
            await p.drain(client)
            assert (await document_text(client, listener)).strip() == TEA
            await assert_search_isolated(client, listener, "翡翠茶", ("金庫", "七九三"))

    asyncio.run(exercise())


def test_receipt_and_expiry_race_projects_latest_range_without_duplicate_append(
    projection: Projection,
) -> None:
    p = projection
    a, _, c = p.characters
    p.post_turn("listener", actor=c, perceived_by=[c], text=MOON, kind="thought")
    listener = p.target("listener")
    full = TEA + "\n" + SECRET
    saved = p.post_turn("spoken", text=full, perceived_by=[a, c])
    p.clock.now += timedelta(seconds=600)
    barrier = Barrier(2)

    def update(entry: str) -> None:
        barrier.wait(timeout=5)
        if entry == "receipt":
            p.service.add_receipt(
                DeliveryReceipt("spoken", "partial", len(TEA), "Web", p.clock.now)
            )
        else:
            p.service.confirm_expired_deliveries()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, ("receipt", "expiry")))

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            await assert_search_isolated(client, listener, "翡翠茶", ("金庫", "七九三"))
            assert (await document_text(client, listener)).count(TEA) == 1
            assert p.http.get("/turns/spoken").json() == saved

    asyncio.run(exercise())


def test_concurrent_registration_enqueues_one_operation_for_same_turn(
    projection: Projection,
) -> None:
    p = projection
    barrier = Barrier(2)
    body = turn_body(
        turn_id="concurrent",
        session_id=p.session,
        actor=p.characters[0],
        perceived_by=[p.characters[0]],
        text=TEA,
    )

    def register() -> dict[str, object]:
        barrier.wait(timeout=5)
        response = p.http.post("/turns", json=body)
        assert response.status_code == 201, response.text
        return cast(dict[str, object], response.json())

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: register(), range(2)))
    assert responses[0] == responses[1]
    assert len(p.operation_ids()) == 1
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM turns").fetchone() == (1,)
