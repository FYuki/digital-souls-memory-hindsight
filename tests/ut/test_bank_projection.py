"""Automatic projection decisions observed at the service's outbox boundary."""

from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt, Session
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from tests.ut.test_delivery_scope import NOW, TEXT, Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


def test_pending_turn_enqueues_only_speaker_full_text() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    saved = service.add_turn(draft())
    assert len(store.outbox) == 1
    operation = next(iter(store.outbox.values()))
    assert operation.content == saved.text
    assert operation.occurred_at == saved.occurred_at


@pytest.mark.parametrize("kind", ["utterance", "observation"])
def test_immediate_projection_uses_perceivers_not_addressees(kind: str) -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock(), reports_delivery=False)
    banks: dict[str, str] = {}
    for character in ("char-a", "char-b", "char-c"):
        service.add_turn(
            replace(
                draft(),
                turn_id=character,
                actor=character,
                kind="thought",
                perceived_by=(character,),
            )
        )
        operations = [value for value in store.outbox.values() if value.turn_id == character]
        assert len(operations) == 1
        banks[character] = operations[0].bank_id
    saved = service.add_turn(replace(draft(), kind=kind))
    added = [operation for operation in store.outbox.values() if operation.turn_id == saved.turn_id]
    assert len(added) == 2
    assert {operation.bank_id for operation in added} == {banks["char-a"], banks["char-b"]}
    assert {operation.content for operation in added} == {saved.text}


def test_thought_enqueues_only_its_owner() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(replace(draft(), kind="thought", perceived_by=("char-a",)))
    assert len(store.outbox) == 1
    assert next(iter(store.outbox.values())).content == TEXT


def test_empty_perception_enqueues_nothing_even_with_character_addressee() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(replace(draft(), actor="user", perceived_by=()))
    assert store.outbox == {}


@pytest.mark.parametrize(("status", "limit"), [("partial", 3), ("full", len(TEXT))])
def test_receipt_enqueues_only_confirmed_prefix_for_other_character(
    status: str, limit: int
) -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(draft())
    before = dict(store.outbox)
    service.add_receipt(DeliveryReceipt("t-1", status, limit, "Web", NOW))
    added = [operation for key, operation in store.outbox.items() if key not in before]
    assert len(added) == 1
    assert added[0].content == TEXT[:limit]
    assert added[0].bank_id != next(iter(before.values())).bank_id
    assert added[0].occurred_at == draft().occurred_at
    assert all(store.outbox[key] == operation for key, operation in before.items())


def test_expiry_enqueues_full_text_once_and_resend_does_not_duplicate_it() -> None:
    clock = Clock()
    store = MemoryStore()
    service = service_with_session(store, clock)
    saved = service.add_turn(draft())
    assert len(store.outbox) == 1
    clock.now += timedelta(seconds=599)
    service.confirm_expired_deliveries()
    assert len(store.outbox) == 1
    clock.now += timedelta(seconds=1)
    service.confirm_expired_deliveries()
    assert len(store.outbox) == 2
    assert {operation.content for operation in store.outbox.values()} == {TEXT}
    before = dict(store.outbox)
    service.confirm_expired_deliveries()
    assert service.add_turn(draft()) == saved
    assert store.outbox == before


def test_character_bank_is_stable_across_sessions_and_similar_ids_are_distinct() -> None:
    from digital_souls_memory_hindsight.evidence.service import EvidenceService

    store = MemoryStore()
    service = EvidenceService(store, frozenset({"char-a", "char_a"}), clock=Clock())
    for session_id in ("s-1", "s_1"):
        service.add_session(Session(session_id, NOW, None, "Web", False))
        for actor in ("char-a", "char_a"):
            service.add_turn(
                replace(
                    draft(),
                    turn_id=f"{session_id}-{actor}",
                    session_id=session_id,
                    actor=actor,
                    perceived_by=(actor,),
                )
            )
    operations = {operation.turn_id: operation for operation in store.outbox.values()}
    assert len(operations) == 4
    a = operations["s-1-char-a"]
    other_session = operations["s_1-char-a"]
    other_character = operations["s-1-char_a"]
    assert a.bank_id == other_session.bank_id
    assert a.bank_id != other_character.bank_id
    assert a.document_id != other_session.document_id
    independent_store = MemoryStore()
    independent_service = EvidenceService(
        independent_store, frozenset({"char-a", "char_a"}), clock=Clock()
    )
    independent_service.add_session(Session("s-1", NOW, None, "Web", False))
    independent_service.add_turn(replace(draft(), perceived_by=("char-a",)))
    independent = next(iter(independent_store.outbox.values()))
    assert independent.bank_id == a.bank_id
    assert independent.document_id == a.document_id


def test_distinct_turns_with_same_text_share_document_without_losing_an_append() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(draft())
    service.add_turn(replace(draft(), turn_id="second"))
    operations = list(store.outbox.values())
    assert len(operations) == 2
    assert operations[0].operation_id != operations[1].operation_id
    assert operations[0].bank_id == operations[1].bank_id
    assert operations[0].document_id == operations[1].document_id
    assert [operation.content for operation in operations] == [TEXT, TEXT]


@pytest.mark.parametrize("entry", ["receipt", "expiry"])
def test_projection_write_failure_rolls_back_delivery_and_receipt_then_can_retry(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    clock = Clock()
    store = MemoryStore()
    service = service_with_session(store, clock)
    saved = service.add_turn(draft())
    before = dict(store.outbox)
    before_scope = service.get_delivery_scope("t-1")
    clock.now += timedelta(seconds=600)

    def perform() -> None:
        if entry == "receipt":
            service.add_receipt(DeliveryReceipt("t-1", "partial", 3, "Web", NOW))
        else:
            service.confirm_expired_deliveries()

    def fail_write(value: object) -> None:
        raise EvidenceUnavailable("synthetic outbox failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "add_retain", fail_write)
        with pytest.raises(EvidenceUnavailable):
            perform()
    assert service.get_delivery_scope("t-1") == before_scope
    assert service.list_receipts("t-1") == ()
    assert service.get_turn("t-1") == saved
    assert store.outbox == before
    perform()
    assert len(store.outbox) == len(before) + 1


def test_repeated_receipt_with_same_range_does_not_enqueue_another_operation() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(draft())
    receipt = DeliveryReceipt("t-1", "partial", 3, "Web", NOW)
    service.add_receipt(receipt)
    assert len(store.outbox) == 2
    before = dict(store.outbox)
    service.add_receipt(replace(receipt, reported_at=NOW + timedelta(seconds=1)))
    assert store.outbox == before


def test_changed_receipts_snapshot_latest_session_text_and_keep_distinct_replace_ids() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(draft())
    service.add_receipt(DeliveryReceipt("t-1", "full", len(TEXT), "Web", NOW))
    service.add_turn(replace(draft(), turn_id="other", kind="observation", text="架空の月。"))
    for limit in (3, len(TEXT), 3, 0):
        status = "none" if not limit else "full" if limit == len(TEXT) else "partial"
        service.add_receipt(DeliveryReceipt("t-1", status, limit, "Web", NOW))
    replacements = [value for value in store.outbox.values() if value.update_mode == "replace"]
    assert len(replacements) == 4
    assert len({value.operation_id for value in replacements}) == 4
    assert [value.content for value in replacements] == [
        TEXT[:3] + "\n\n架空の月。",
        TEXT + "\n\n架空の月。",
        TEXT[:3] + "\n\n架空の月。",
        "架空の月。",
    ]
    assert len({(value.bank_id, value.document_id) for value in replacements}) == 1
    assert service.get_turn("t-1").text == TEXT
