"""The ledger owns atomic registration of explicit retain operations."""

import importlib
from dataclasses import replace

import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceConflict, EvidenceUnavailable
from tests.ut.test_delivery_scope import Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


def test_turn_and_explicit_operations_are_idempotent_and_keep_event_time() -> None:
    outbox = importlib.import_module("digital_souls_memory_hindsight.outbox")
    store = MemoryStore()
    service = service_with_session(store, Clock())
    target = outbox.RetainTarget("bank-a", "session-1", "架空のお茶")
    saved = service.add_turn(draft(), retain_targets=(target,))
    assert service.add_turn(draft(), retain_targets=(target,)) == saved
    assert len(store.outbox) == 1
    operation = next(iter(store.outbox.values()))
    assert operation.occurred_at == saved.occurred_at != saved.recorded_at
    assert operation.content == target.content
    assert (operation.bank_id, operation.document_id) == ("bank-a", "session-1")
    with pytest.raises(EvidenceConflict):
        service.add_turn(replace(draft(), text="違う本文"), retain_targets=(target,))
    with pytest.raises(EvidenceConflict):
        service.add_turn(draft(), retain_targets=(replace(target, content="異なる投影"),))
    assert len(store.outbox) == 1
    assert next(iter(store.outbox.values())) == operation


def test_outbox_write_failure_rolls_back_turn_and_delivery_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = importlib.import_module("digital_souls_memory_hindsight.outbox")
    store = MemoryStore()
    service = service_with_session(store, Clock())
    target = outbox.RetainTarget("bank-a", "session-1", "架空のお茶")

    def fail_write(value: object) -> None:
        raise EvidenceUnavailable()

    with monkeypatch.context() as patch:
        patch.setattr(store, "add_retain", fail_write)
        with pytest.raises(EvidenceUnavailable):
            service.add_turn(draft(), retain_targets=(target,))
    assert store.get_turn("t-1") is None
    assert store.delivery_scopes == []
    assert store.outbox == {}
    service.add_turn(draft(), retain_targets=(target,))
    assert store.get_turn("t-1") is not None
    assert len(store.outbox) == 1
