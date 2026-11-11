"""Recall storage work stays bounded as anchors, documents and turns grow."""

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from digital_souls_memory_hindsight.anchors.service import AnchorService
from digital_souls_memory_hindsight.evidence.domain import Session
from digital_souls_memory_hindsight.evidence.projection import document_id
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.recall.ports import RecalledFact
from digital_souls_memory_hindsight.recall.service import RecallService
from tests.ut.test_anchor_http import AnchorStore
from tests.ut.test_deletion_http import DeletionStore
from tests.ut.test_delivery_scope import NOW, Clock, draft
from tests.ut.test_evidence_http import MemoryStore
from tests.ut.test_recall_http import ProvisionStore

pytestmark = pytest.mark.ut


class FixedRecall:
    def __init__(self, facts: tuple[RecalledFact, ...]) -> None:
        self.facts = facts

    async def recall(self, bank_id: str, query: str) -> tuple[RecalledFact, ...]:
        return self.facts


def seed_recall(
    evidence: EvidenceService,
    anchors: AnchorService,
    anchor_count: int,
    documents: int,
    turns: int,
    *,
    prefix: str,
) -> tuple[RecalledFact, ...]:
    facts = []
    identifiers = []
    for document in range(documents):
        session_id = f"{prefix}s-{document}"
        evidence.add_session(Session(session_id, NOW, None, "Web", False))
        for index in range(turns):
            identifier = f"{prefix}t-{document}-{index}"
            identifiers.append(identifier)
            text = f"架空の約束 {document} 番 {index} 番の原文です。"
            evidence.add_turn(
                replace(
                    draft(),
                    turn_id=identifier,
                    session_id=session_id,
                    text=text,
                    perceived_by=("char-a", "char-c"),
                )
            )
            facts.append(RecalledFact(identifier, "world", text, document_id(session_id), text))
    for index in range(anchor_count):
        anchors.register(
            "char-c",
            f"{prefix}anchor-{index:02}",
            identifiers[index % len(identifiers)],
            "commitment",
            "架空の約束",
            "synthetic-author",
        )
    return tuple(facts)


@pytest.mark.parametrize("large", [(20, 1, 1), (2, 4, 1), (2, 1, 5), (20, 4, 5)])
def test_recall_read_calls_do_not_grow_with_anchors_documents_or_turns(
    monkeypatch: pytest.MonkeyPatch, large: tuple[int, int, int]
) -> None:
    def measure(size: tuple[int, int, int]) -> int:
        store = MemoryStore()
        evidence = EvidenceService(store, frozenset({"char-a", "char-c"}), clock=Clock())
        anchor_store = AnchorStore()
        anchors = AnchorService(anchor_store, evidence, clock=Clock())
        facts = seed_recall(evidence, anchors, *size, prefix="")
        provisions = ProvisionStore()
        deletions = DeletionStore()
        service = RecallService(evidence, FixedRecall(facts), provisions, anchors, deletions)
        spies = []
        for owner, names in (
            (
                store,
                (
                    "get_session",
                    "get_turn",
                    "get_delivery_scope",
                    "list_projection_sessions",
                    "list_projection_turns",
                    "get_turns",
                    "get_delivery_scopes",
                    "list_projection_turns_for_sessions",
                ),
            ),
            (anchor_store, ("list", "match")),
            (deletions, ("blocked_sessions",)),
        ):
            for name in names:
                spy = MagicMock(wraps=getattr(owner, name))
                monkeypatch.setattr(owner, name, spy)
                spies.append(spy)
        result = asyncio.run(
            service.recall("char-c", "s-0", "assembled", "約束", None, "public", "fixture-1", None)
        )
        assert tuple(m.id for m in result.memories) == tuple(f.id for f in facts)
        assert tuple(s.text for s in result.source_spans) == tuple(f.chunk_text for f in facts)
        assert tuple(a.anchor_id for a in result.verbatim_anchors) == tuple(
            f"anchor-{index:02}" for index in range(size[0])
        )
        record = provisions.records[result.provision_id]
        assert record.kind == "turn"
        assert record.memory_ids == tuple(m.id for m in result.memories)
        assert tuple(s.turn_id for s in record.source_spans) == tuple(
            s.turn_id for s in result.source_spans
        )
        assert record.verbatim_anchor_ids == tuple(a.anchor_id for a in result.verbatim_anchors)
        return sum(spy.call_count for spy in spies)

    small_count = measure((2, 1, 1))
    large_count = measure(large)
    assert large_count == small_count, (small_count, large_count)
