"""The ledger owns atomic registration of automatic retain operations."""

from dataclasses import replace

import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceConflict, EvidenceUnavailable
from tests.ut.test_delivery_scope import Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


def test_automatic_operation_is_idempotent_and_keeps_event_time() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    saved = service.add_turn(draft())
    assert service.add_turn(draft()) == saved
    assert len(store.outbox) == 1
    operation = next(iter(store.outbox.values()))
    assert operation.occurred_at == saved.occurred_at != saved.recorded_at
    assert operation.content == saved.text
    with pytest.raises(EvidenceConflict):
        service.add_turn(replace(draft(), text="違う本文"))
    assert len(store.outbox) == 1
    assert next(iter(store.outbox.values())) == operation


def test_outbox_write_failure_rolls_back_turn_and_delivery_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())

    def fail_write(value: object) -> None:
        raise EvidenceUnavailable()

    with monkeypatch.context() as patch:
        patch.setattr(store, "add_retain", fail_write)
        with pytest.raises(EvidenceUnavailable):
            service.add_turn(draft())
    assert store.get_turn("t-1") is None
    assert store.delivery_scopes == []
    assert store.outbox == {}
    service.add_turn(draft())
    assert store.get_turn("t-1") is not None
    assert len(store.outbox) == 1
