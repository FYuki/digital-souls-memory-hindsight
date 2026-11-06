"""Real ledger connections, delivery boundaries and deletion during recall."""

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg.rows import dict_row

from digital_souls_memory_hindsight.anchors.service import AnchorService
from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt, Session
from digital_souls_memory_hindsight.evidence.projection import document_id
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.postgres_anchors import PostgresAnchorStore
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_provision import PostgresProvisionStore
from digital_souls_memory_hindsight.recall.ports import MemoryRecall, RecalledFact
from digital_souls_memory_hindsight.recall.service import RecallService
from digital_souls_memory_hindsight.recall_http import recall_router
from tests.ut.test_delivery_scope import NOW, Clock, draft
from tests.ut.test_recall_query_counts import FixedRecall, seed_recall

pytestmark = pytest.mark.it1


def services(database: str) -> tuple[EvidenceService, AnchorService, DeletionService]:
    evidence = EvidenceService(
        PostgresEvidenceStore(database), frozenset({"char-a", "char-c", "char-b"}), clock=Clock()
    )
    return (
        evidence,
        AnchorService(PostgresAnchorStore(database), evidence, clock=Clock()),
        DeletionService(PostgresDeletionStore(database), evidence, clock=Clock()),
    )


async def recall_http(
    database: str,
    evidence: EvidenceService,
    anchors: AnchorService,
    deletions: DeletionService,
    memory: MemoryRecall,
    session: str,
) -> httpx.Response:
    service = RecallService(
        evidence, memory, PostgresProvisionStore(database), anchors, deletions, clock=Clock()
    )
    app = FastAPI()
    app.include_router(recall_router(lambda: service))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    ) as client:
        return await client.post(
            "/characters/char-c/recall",
            json={
                "session_id": session,
                "query": "約束",
                "format": "assembled",
                "sensitivity": "public",
                "classifier_version": "fixture-1",
            },
        )


def test_recall_connection_count_is_constant_for_twenty_anchors_and_multiple_documents(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = []
    real_connect = psycopg.connect
    for prefix, size in (("small-", (2, 1, 1)), ("large-", (20, 4, 5))):
        evidence, anchors, deletions = services(database)
        facts = seed_recall(evidence, anchors, *size, prefix=prefix)
        # Keep the two measured data sets separate without bypassing the store's filters.
        with real_connect(database, connect_timeout=5) as connection:
            connection.execute(
                "DELETE FROM verbatim_anchors WHERE anchor_id NOT LIKE %s", (prefix + "%",)
            )
        with monkeypatch.context() as patch:
            connect = MagicMock(wraps=real_connect)
            patch.setattr(psycopg, "connect", connect)
            response = asyncio.run(
                recall_http(
                    database, evidence, anchors, deletions, FixedRecall(facts), prefix + "s-0"
                )
            )
            counts.append(connect.call_count)
        assert response.status_code == 200, response.text
        body = response.json()
        assert [m["id"] for m in body["memories"]] == [f.id for f in facts]
        assert [s["text"] for s in body["source_spans"]] == [f.chunk_text for f in facts]
        assert [a["anchor_id"] for a in body["verbatim_anchors"]] == [
            f"{prefix}anchor-{index:02}" for index in range(size[0])
        ]
        # A fresh connection can observe the committed record immediately after HTTP returns.
        with real_connect(database, row_factory=dict_row, connect_timeout=5) as connection:
            row = connection.execute(
                "SELECT kind, memory_ids, source_spans, verbatim_anchor_ids FROM provision_records "
                "WHERE provision_id = %s",
                (body["provision_id"],),
            ).fetchone()
        assert row == {
            "kind": "turn",
            "memory_ids": [m["id"] for m in body["memories"]],
            "source_spans": [
                {k: s[k] for k in ("turn_id", "start", "end")} for s in body["source_spans"]
            ],
            "verbatim_anchor_ids": [a["anchor_id"] for a in body["verbatim_anchors"]],
        }
    assert counts[1] == counts[0], counts
    assert counts[0] == 2, "One read connection and one committed provision connection"


def test_deletion_committed_while_memory_recall_waits_blocks_facts_and_anchors(
    database: str,
) -> None:
    evidence, anchors, deletions = services(database)
    facts = seed_recall(evidence, anchors, 2, 2, 1, prefix="")

    async def exercise() -> httpx.Response:
        entered, release = asyncio.Event(), asyncio.Event()

        class WaitingRecall:
            async def recall(self, bank_id: str, query: str) -> tuple[RecalledFact, ...]:
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=5)
                return facts

        task = asyncio.create_task(
            recall_http(database, evidence, anchors, deletions, WaitingRecall(), "s-0")
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await asyncio.to_thread(
                deletions.accept, "delete-during-recall", "synthetic-author", ("t-0-0",), None
            )
        finally:
            release.set()
        return await task

    response = asyncio.run(exercise())
    assert response.status_code == 200, response.text
    body = response.json()
    assert [m["id"] for m in body["memories"]] == ["t-1-0"]
    assert [s["turn_id"] for s in body["source_spans"]] == ["t-1-0"]
    assert [a["turn_id"] for a in body["verbatim_anchors"]] == ["t-1-0"]


def test_projection_sources_keep_outbox_order_latest_delivery_and_perception(database: str) -> None:
    evidence, _, _ = services(database)
    for session in ("z-session", "a-session"):
        evidence.add_session(Session(session, NOW, None, "Web", True))
    text = "架空の約束🍵。非公開の続き"
    for identifier, session, perceived in (
        ("z-first", "z-session", ("char-a", "char-c")),
        ("a-middle", "a-session", ("char-a", "char-c")),
        ("a-last", "z-session", ("char-a", "char-c")),
        ("hidden", "z-session", ("char-a", "char-b")),
    ):
        evidence.add_turn(
            replace(
                draft(), turn_id=identifier, session_id=session, text=text, perceived_by=perceived
            )
        )
        evidence.add_receipt(DeliveryReceipt(identifier, "full", len(text), "Web", NOW))
        evidence.add_receipt(DeliveryReceipt(identifier, "partial", 8, "Web", NOW))
    sources = evidence.projection_sources(
        "char-c",
        frozenset(
            {
                document_id("z-session"),
                document_id("a-session"),
            }
        ),
    )
    assert {
        key: [(turn.turn_id, projected) for turn, projected in values]
        for key, values in sources.items()
    } == {
        document_id("z-session"): [("z-first", text[:8]), ("a-last", text[:8])],
        document_id("a-session"): [("a-middle", text[:8])],
    }
