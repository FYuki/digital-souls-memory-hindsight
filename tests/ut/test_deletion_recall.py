"""Deletion acceptance changes subsequent recall on the same live service."""

import importlib
from typing import TypedDict, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.projection import document_id
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from tests.ut.test_anchor_http import AnchorStore, anchor_body
from tests.ut.test_deletion_http import AUTH, DeletionStore
from tests.ut.test_delivery_scope import Clock
from tests.ut.test_evidence_http import MemoryStore, session_body, turn_body
from tests.ut.test_recall_http import MOON, TEA, MemoryRecall, ProvisionStore, recall_body

pytestmark = pytest.mark.ut


class MemorySummary(TypedDict):
    id: str


class SpanSummary(TypedDict):
    turn_id: str


class AnchorSummary(TypedDict):
    anchor_id: str


class RecallContent(TypedDict):
    memories: list[MemorySummary]
    source_spans: list[SpanSummary]
    verbatim_anchors: list[AnchorSummary]
    context: str | None


@pytest.mark.parametrize("terminal", ["failed", "completed"])
@pytest.mark.parametrize("with_facts", [False, True])
@pytest.mark.parametrize("format", ["materials", "assembled"])
def test_acceptance_blocks_entire_source_session_and_records_until_completed(
    terminal: str, with_facts: bool, format: str
) -> None:
    deletion = importlib.import_module("digital_souls_memory_hindsight.deletion.service")
    deletion_http = importlib.import_module("digital_souls_memory_hindsight.deletion_http")
    recall = importlib.import_module("digital_souls_memory_hindsight.recall.service")
    recall_http = importlib.import_module("digital_souls_memory_hindsight.recall_http")
    ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
    anchor = importlib.import_module("digital_souls_memory_hindsight.anchors.service")
    anchor_http = importlib.import_module("digital_souls_memory_hindsight.anchor_http")
    evidence = EvidenceService(MemoryStore(), frozenset({"char-a", "char-b", "char-c"}))
    store = DeletionStore()
    deletions = deletion.DeletionService(store, evidence, clock=Clock())
    memory = MemoryRecall()
    provisions = ProvisionStore()
    anchors = anchor.AnchorService(AnchorStore(), evidence)
    instance = recall.RecallService(evidence, memory, provisions, anchors, deletions, clock=Clock())
    app = FastAPI()
    app.include_router(evidence_router(lambda: evidence))
    app.include_router(recall_http.recall_router(lambda: instance))
    app.include_router(anchor_http.anchor_router(lambda: anchors))
    app.include_router(
        deletion_http.deletion_router(
            lambda: deletions,
            deletion_http.AuthorCredentials("synthetic-author", "synthetic-deletion-token"),
        )
    )
    if with_facts:
        memory.facts = tuple(
            ports.RecalledFact(
                id=identifier,
                kind="world",
                text=text,
                document_id=document_id(session),
                chunk_text=text,
            )
            for identifier, session, text in (
                ("blocked-fact", "s-1", TEA),
                ("same-session-fact", "s-1", MOON),
                ("kept-fact", "s-2", MOON),
            )
        )
    with TestClient(app) as client:
        for session in ("s-1", "s-2"):
            assert (
                client.post(
                    "/sessions", json=session_body(session_id=session, reports_delivery=False)
                ).status_code
                == 201
            )
        for identifier, session, text, perceived in (
            ("t-1", "s-1", TEA, ["char-a", "char-c"]),
            ("same-session", "s-1", MOON, ["char-a", "char-b", "char-c"]),
            ("kept", "s-2", MOON, ["char-a"]),
        ):
            assert (
                client.post(
                    "/turns",
                    json=turn_body(
                        turn_id=identifier, session_id=session, text=text, perceived_by=perceived
                    ),
                ).status_code
                == 201
            )
        for character in ("char-a", "char-c"):
            for identifier, turn in (("blocked-anchor", "t-1"), ("same-anchor", "same-session")):
                response = client.post(
                    f"/characters/{character}/verbatim-anchors",
                    json=anchor_body(anchor_id=identifier, turn_id=turn),
                )
                assert response.status_code == 201, response.text

        def observe(character: str) -> RecallContent:
            response = client.post(
                f"/characters/{character}/recall", json=recall_body(query="架空", format=format)
            )
            assert response.status_code == 200, response.text
            body = response.json()
            record = provisions.records[body.pop("provision_id")]
            assert record.memory_ids == tuple(m["id"] for m in body["memories"])
            assert tuple(s.turn_id for s in record.source_spans) == tuple(
                s["turn_id"] for s in body["source_spans"]
            )
            assert record.verbatim_anchor_ids == tuple(
                a["anchor_id"] for a in body["verbatim_anchors"]
            )
            return cast(RecallContent, body)

        before = {character: observe(character) for character in ("char-a", "char-b", "char-c")}
        assert before["char-a"]["verbatim_anchors"]
        accepted = client.post(
            "/deletion-requests", headers=AUTH, json={"request_id": "d", "target_turn_ids": ["t-1"]}
        )
        assert accepted.status_code == 202, accepted.text
        for state in ("accepted", "processing", terminal):
            if state != "accepted":
                deletions.transition(
                    "d", state, error="synthetic failure" if state == "failed" else None
                )
            for character in ("char-a", "char-c"):
                body = observe(character)
                if state == "completed":
                    assert body == before[character]
                else:
                    expected_memories = (
                        ["kept-fact"] if character == "char-a" and with_facts else []
                    )
                    expected_turns = ["kept"] if expected_memories else []
                    assert [m["id"] for m in body["memories"]] == expected_memories
                    assert [s["turn_id"] for s in body["source_spans"]] == expected_turns
                    assert body["verbatim_anchors"] == []
                    assert body["context"] == (
                        (MOON if expected_turns else "") if format == "assembled" else None
                    )
            assert observe("char-b") == before["char-b"]
        saved_count = len(provisions.records)
        store.error = EvidenceUnavailable("synthetic private deletion database failure")
        failed = client.post("/characters/char-a/recall", json=recall_body())
        assert failed.status_code == 503
        assert failed.json() == {"detail": "Recall unavailable"}
        assert len(provisions.records) == saved_count
