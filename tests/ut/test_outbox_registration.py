"""The ledger owns atomic registration of automatic retain operations."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.ports import EvidenceConflict, EvidenceUnavailable
from tests.ut.test_delivery_scope import Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore, application, session_body, turn_body

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


@pytest.mark.parametrize("after_lock", [False, True])
@pytest.mark.parametrize("changed_text", [False, True])
def test_tombstoned_turn_is_rejected_without_recreating_evidence_or_projection(
    after_lock: bool, changed_text: bool
) -> None:
    class TombstonedStore(MemoryStore):
        def lock_session(self, session_id: str) -> None:
            super().lock_session(session_id)
            if after_lock:
                self.tombstones.add("t-1")

    store = TombstonedStore()
    if not after_lock:
        store.tombstones.add("t-1")
    with TestClient(application(frozenset({"char-a", "char-b"}), store)) as client:
        assert client.post("/sessions", json=session_body()).status_code == 201
        body = turn_body(text="合成の再送本文") if changed_text else turn_body()
        response = client.post("/turns", json=body)
        assert response.status_code == 409
    assert store.turns == {}
    assert store.receipts == []
    assert store.delivery_scopes == []
    assert store.outbox == {}


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
