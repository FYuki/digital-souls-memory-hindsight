"""Source matching and ledger reads retain document and perception boundaries."""

from dataclasses import replace

import pytest

from digital_souls_memory_hindsight.evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
)
from digital_souls_memory_hindsight.evidence.ports import EvidenceNotFound, EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.projection import document_id
from digital_souls_memory_hindsight.recall.service import match_sources
from tests.ut.test_delivery_scope import NOW, Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    ("projected", "chunk", "matches"),
    [
        ("abcdefghijk", "prefix-abcdefghijk-suffix", True),
        ("abcdefghijk", "cdefghij", True),
        ("abcdefghijk", "prefix-abcdefgh", True),
        ("abcdefghijk", "defghijk-suffix", True),
        ("abcdefghijk", "prefix-abcdefg", False),
        ("abcdefghijk", "efghijk-suffix", False),
        ("abcdefghijk", "unrelated", False),
        (" abcd\nefghijk ", "prefix-abcdefgh", True),
        ("abcdefghijk", " \n ", False),
        (" \t ", "chunk", False),
    ],
)
def test_chunk_overlap_preserves_original_text_and_rejects_short_boundary_coincidences(
    projected: str,
    chunk: str,
    matches: bool,
) -> None:
    service = service_with_session(MemoryStore(), Clock())
    turn = service.add_turn(replace(draft(), text=projected))
    candidates = ((turn, projected),)
    assert match_sources(chunk, candidates) == (candidates if matches else ())


def test_projection_sources_filter_documents_and_read_latest_delivery() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    turn = service.add_turn(draft())
    service.add_session(Session("s-2", NOW, None, "Web", False))
    service.add_turn(replace(draft(), session_id="s-2", turn_id="other", text="other session"))
    documents = frozenset({document_id("s-1")})
    assert service.projection_sources("char-b", documents) == {}
    service.add_receipt(DeliveryReceipt("t-1", "full", len(turn.text), "Web", NOW))
    assert service.projection_sources("char-b", documents) == {
        document_id("s-1"): ((turn, turn.text),)
    }
    service.add_receipt(DeliveryReceipt("t-1", "partial", 3, "Web", NOW))
    assert service.projection_sources("char-b", documents) == {
        document_id("s-1"): ((turn, turn.text[:3]),)
    }
    service.add_receipt(DeliveryReceipt("t-1", "none", 0, "Web", NOW))
    assert service.projection_sources("char-b", documents) == {document_id("s-1"): ()}
    assert service.projection_sources("char-c", documents) == {}


@pytest.mark.parametrize(
    ("character", "kind", "delivered"),
    [
        ("char-a", "utterance", None),
        ("char-b", "utterance", None),
        ("char-b", "utterance", 0),
        ("char-b", "utterance", 3),
        ("char-c", "utterance", 3),
        ("char-a", "thought", None),
        ("char-b", "thought", None),
    ],
)
def test_batch_projections_preserve_single_turn_delivery_and_perception(
    character: str, kind: str, delivered: int | None
) -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    value = replace(
        draft(), kind=kind, perceived_by=("char-a",) if kind == "thought" else ("char-a", "char-b")
    )
    turn = service.add_turn(value)
    if delivered is not None:
        service.add_receipt(
            DeliveryReceipt(
                turn.turn_id, "none" if delivered == 0 else "partial", delivered, "Web", NOW
            )
        )
    expected = service.get_projection_text(turn.turn_id, character)
    assert service.projections(frozenset({turn.turn_id}), character) == {
        turn.turn_id: (turn, expected)
    }
    assert service.projections(frozenset(), character) == {}


def test_batch_projection_missing_turn_and_scope_preserve_errors() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    turn = service.add_turn(draft())
    with pytest.raises(EvidenceNotFound):
        service.projections(frozenset({turn.turn_id, "absent"}), "char-b")
    store.delivery_scopes.clear()
    with pytest.raises(EvidenceUnavailable):
        service.projections(frozenset({turn.turn_id}), "char-b")
    assert service.projections(frozenset({turn.turn_id}), "char-a") == {
        turn.turn_id: (turn, turn.text)
    }
    # The batch path must not turn inconsistent thought perception into disclosed text.
    store.turns[turn.turn_id] = replace(turn, kind="thought")
    with pytest.raises(EvidenceValidationError):
        service.projections(frozenset({turn.turn_id}), "char-b")
