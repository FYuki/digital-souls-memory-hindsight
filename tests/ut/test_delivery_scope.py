"""Projection queries follow the same ledger turn across delivery changes."""

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from digital_souls_memory_hindsight.evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
    TurnInput,
)
from digital_souls_memory_hindsight.evidence.ports import EvidenceNotFound, EvidenceUnavailable
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut
NOW = datetime(2026, 10, 28, tzinfo=UTC)
TEXT = " 茶🍵。秘密\n"
CHARACTERS = frozenset({"char-a", "char-b", "char-c"})


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


def draft() -> TurnInput:
    return TurnInput(
        "t-1",
        "s-1",
        "Web",
        "char-a",
        "utterance",
        TEXT,
        ("char-a", "char-b"),
        ("char-c",),
        "direct",
        None,
        "ja",
        {},
        "public",
        "fixture-1",
        NOW - timedelta(days=1),
    )


def service_with_session(
    store: MemoryStore, clock: Clock, *, reports_delivery: bool = True
) -> EvidenceService:
    service = EvidenceService(store, CHARACTERS, clock=clock)
    service.add_session(Session("s-1", NOW - timedelta(days=2), None, "Web", reports_delivery))
    return service


@pytest.mark.parametrize(
    ("reports_delivery", "actor", "kind"),
    [
        (False, "char-a", "utterance"),
        (True, "user-a", "utterance"),
        (True, "char-a", "observation"),
        (True, "user-a", "observation"),
    ],
)
def test_turns_without_receipt_wait_are_immediately_projectable(
    reports_delivery: bool, actor: str, kind: str
) -> None:
    service = service_with_session(MemoryStore(), Clock(), reports_delivery=reports_delivery)
    service.add_turn(replace(draft(), actor=actor, kind=kind))
    scope = service.get_delivery_scope("t-1")
    assert scope is not None
    assert scope.spoken_until == len(TEXT)
    assert service.get_projection_text("t-1", "char-b") == TEXT
    assert service.get_projection_text("t-1", "char-c") is None


@pytest.mark.parametrize(("status", "limit"), [("partial", 3), ("none", 0), ("full", len(TEXT))])
def test_pending_turn_follows_receipt_range_without_changing_original(
    status: str, limit: int
) -> None:
    clock = Clock()
    store = MemoryStore()
    service = service_with_session(store, clock)
    saved = service.add_turn(draft())
    pending = service.get_delivery_scope("t-1")
    assert pending is not None
    assert pending.spoken_until is None
    assert service.get_projection_text("t-1", "char-b") is None
    assert service.get_projection_text("t-1", "char-a") == TEXT
    clock.now += timedelta(seconds=599)
    service.add_receipt(DeliveryReceipt("t-1", status, limit, "Web", NOW - timedelta(days=3)))
    confirmed = service.get_delivery_scope("t-1")
    assert confirmed is not None
    assert confirmed.status != pending.status
    assert confirmed.spoken_until == limit
    assert service.get_projection_text("t-1", "char-b") == TEXT[:limit]
    assert service.get_projection_text("t-1", "char-a") == TEXT
    assert service.get_projection_text("t-1", "char-c") is None
    assert service.get_turn("t-1") == saved
    clock.now += timedelta(days=1)
    service.confirm_expired_deliveries()
    assert service.get_delivery_scope("t-1") == confirmed
    assert service.get_projection_text("t-1", "char-b") == TEXT[:limit]
    assert len(store.delivery_scopes) == 2


def test_default_deadline_uses_recorded_time_and_is_idempotent() -> None:
    clock = Clock()
    store = MemoryStore()
    service = service_with_session(store, clock)
    saved = service.add_turn(draft())
    assert saved.recorded_at == NOW
    pending = service.get_delivery_scope("t-1")
    clock.now = NOW + timedelta(seconds=600) - timedelta(microseconds=1)
    assert tuple(service.confirm_expired_deliveries()) == ()
    assert service.get_delivery_scope("t-1") == pending
    assert service.get_projection_text("t-1", "char-b") is None
    assert service.add_turn(draft()) == saved
    assert service.get_delivery_scope("t-1") == pending
    assert len(store.delivery_scopes) == 1
    clock.now = NOW + timedelta(seconds=600)
    assert tuple(service.confirm_expired_deliveries()) == ("t-1",)
    assumed = service.get_delivery_scope("t-1")
    assert assumed is not None and pending is not None
    assert assumed.status != pending.status
    assert assumed.spoken_until == len(TEXT)
    assert service.get_projection_text("t-1", "char-b") == TEXT
    assert service.list_receipts("t-1") == ()
    assert tuple(service.confirm_expired_deliveries()) == ()
    assert len(store.delivery_scopes) == 2
    assert service.add_turn(draft()) == saved
    assert service.get_delivery_scope("t-1") == assumed
    assert service.get_turn("t-1") == saved


def test_four_delivery_states_remain_distinguishable() -> None:
    clock = Clock()
    service = service_with_session(MemoryStore(), clock)
    service.add_turn(draft())
    pending = service.get_delivery_scope("t-1")
    service.add_turn(replace(draft(), turn_id="immediate", actor="user-a"))
    immediate = service.get_delivery_scope("immediate")
    service.add_turn(replace(draft(), turn_id="received"))
    service.add_receipt(DeliveryReceipt("received", "full", len(TEXT), "Web", NOW))
    received = service.get_delivery_scope("received")
    clock.now += timedelta(seconds=600)
    service.confirm_expired_deliveries()
    assumed = service.get_delivery_scope("t-1")
    assert all(scope is not None for scope in (pending, immediate, received, assumed))
    assert len({scope.status for scope in (pending, immediate, received, assumed) if scope}) == 4
    assert service.get_delivery_scope("immediate") == immediate
    assert service.get_delivery_scope("received") == received


def test_expiry_leaves_more_recent_pending_turns_untouched() -> None:
    clock = Clock()
    service = service_with_session(MemoryStore(), clock)
    service.add_turn(draft())
    clock.now += timedelta(seconds=30)
    service.add_turn(replace(draft(), turn_id="recent"))
    recent = service.get_delivery_scope("recent")
    clock.now = NOW + timedelta(seconds=600)
    assert tuple(service.confirm_expired_deliveries()) == ("t-1",)
    assert service.get_projection_text("t-1", "char-b") == TEXT
    assert service.get_projection_text("recent", "char-b") is None
    assert service.get_delivery_scope("recent") == recent


def test_late_and_multiple_receipts_use_acceptance_order_and_preserve_history() -> None:
    clock = Clock()
    store = MemoryStore()
    service = service_with_session(store, clock)
    service.add_turn(draft())
    clock.now += timedelta(seconds=600)
    service.confirm_expired_deliveries()
    history = tuple(store.delivery_scopes)
    assumed = service.get_delivery_scope("t-1")
    service.add_receipt(DeliveryReceipt("t-1", "partial", 3, "Web", clock.now))
    received = service.get_delivery_scope("t-1")
    assert received is not None and assumed is not None
    assert received.status != assumed.status
    assert service.get_projection_text("t-1", "char-b") == TEXT[:3]
    service.add_receipt(DeliveryReceipt("t-1", "none", 0, "Web", NOW))
    assert service.get_projection_text("t-1", "char-b") == ""
    assert tuple(store.delivery_scopes[:2]) == history
    assert len(store.delivery_scopes) == 4


@pytest.mark.parametrize(("actor", "kind"), [("user-a", "utterance"), ("char-a", "observation")])
def test_receipts_for_nonwaiting_turns_preserve_immediate_full_text(actor: str, kind: str) -> None:
    service = service_with_session(MemoryStore(), Clock())
    service.add_turn(replace(draft(), actor=actor, kind=kind))
    initial = service.get_delivery_scope("t-1")
    receipt = DeliveryReceipt("t-1", "none", 0, "Web", NOW)
    assert service.add_receipt(receipt) == receipt
    assert service.list_receipts("t-1") == (receipt,)
    assert service.get_delivery_scope("t-1") == initial
    assert service.get_projection_text("t-1", "char-b") == TEXT


@pytest.mark.parametrize("reports_delivery", [False, True])
def test_thought_has_no_delivery_scope_and_only_actor_gets_text(reports_delivery: bool) -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock(), reports_delivery=reports_delivery)
    service.add_turn(replace(draft(), kind="thought", perceived_by=("char-a",)))
    assert service.get_delivery_scope("t-1") is None
    assert store.delivery_scopes == []
    assert service.get_projection_text("t-1", "char-a") == TEXT
    assert service.get_projection_text("t-1", "char-b") is None
    assert service.get_projection_text("t-1", "char-c") is None


@pytest.mark.parametrize(("reports_delivery", "kind"), [(False, "utterance"), (True, "thought")])
def test_rejected_receipt_does_not_append_delivery_record(
    reports_delivery: bool, kind: str
) -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock(), reports_delivery=reports_delivery)
    saved = service.add_turn(replace(draft(), kind=kind, perceived_by=("char-a",)))
    before = tuple(store.delivery_scopes)
    before_outbox = dict(store.outbox)
    with pytest.raises(EvidenceValidationError):
        service.add_receipt(DeliveryReceipt("t-1", "none", 0, "Web", NOW))
    assert tuple(store.delivery_scopes) == before
    assert service.list_receipts("t-1") == ()
    assert service.get_turn("t-1") == saved
    assert store.outbox == before_outbox


@pytest.mark.parametrize("operation", ["turn", "receipt", "expiry"])
def test_delivery_write_failure_rolls_back_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store = MemoryStore()
    clock = Clock()
    service = service_with_session(store, clock)
    if operation != "turn":
        service.add_turn(draft())
    before = tuple(store.delivery_scopes)
    clock.now += timedelta(seconds=600)
    receipt = DeliveryReceipt("t-1", "partial", 3, "Web", NOW)

    def perform() -> None:
        if operation == "turn":
            service.add_turn(draft())
        elif operation == "receipt":
            service.add_receipt(receipt)
        else:
            service.confirm_expired_deliveries()

    def fail_write(value: object) -> None:
        raise EvidenceUnavailable("synthetic delivery write failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "add_delivery_scope", fail_write)
        with pytest.raises(EvidenceUnavailable):
            perform()
    assert tuple(store.delivery_scopes) == before
    assert store.list_receipts("t-1") == ()
    if operation == "turn":
        assert store.get_turn("t-1") is None
    else:
        assert service.get_projection_text("t-1", "char-b") is None
    perform()
    assert len(store.delivery_scopes) == len(before) + 1
    expected = None if operation == "turn" else TEXT[:3] if operation == "receipt" else TEXT
    assert service.get_projection_text("t-1", "char-b") == expected


def test_projection_lookup_of_missing_turn_is_not_found() -> None:
    service = service_with_session(MemoryStore(), Clock())
    with pytest.raises(EvidenceNotFound):
        service.get_projection_text("absent", "char-b")


def test_missing_delivery_record_does_not_assume_full_text() -> None:
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(draft())
    store.delivery_scopes.clear()
    with pytest.raises(EvidenceUnavailable):
        service.get_delivery_scope("t-1")
    with pytest.raises(EvidenceUnavailable):
        service.get_projection_text("t-1", "char-b")
    with pytest.raises(EvidenceUnavailable):
        service.add_receipt(DeliveryReceipt("t-1", "full", len(TEXT), "Web", NOW))
    assert store.list_receipts("t-1") == ()


@pytest.mark.parametrize("timeout", [0, -1])
def test_service_rejects_nonpositive_delivery_timeout(timeout: int) -> None:
    with pytest.raises(ValueError):
        EvidenceService(MemoryStore(), CHARACTERS, delivery_timeout_seconds=timeout)


def test_configured_timeout_changes_the_actual_confirmation_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from digital_souls_memory_hindsight.settings import load_settings

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_DELIVERY_TIMEOUT_SECONDS", "42")
    settings = load_settings()
    clock = Clock()
    service = EvidenceService(
        MemoryStore(),
        CHARACTERS,
        clock=clock,
        delivery_timeout_seconds=settings.delivery_timeout_seconds,
    )
    service.add_session(Session("s-1", NOW, None, "Web", True))
    service.add_turn(draft())
    clock.now += timedelta(seconds=41)
    assert tuple(service.confirm_expired_deliveries()) == ()
    assert service.get_projection_text("t-1", "char-b") is None
    clock.now += timedelta(seconds=1)
    assert tuple(service.confirm_expired_deliveries()) == ("t-1",)
    assert service.get_projection_text("t-1", "char-b") == TEXT


def test_application_passes_configured_timeout_to_live_service(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
from datetime import UTC, datetime, timedelta
from fastapi.testclient import TestClient
from tests.ut.test_evidence_http import MemoryStore, session_body, turn_body
import digital_souls_memory_hindsight.postgres_evidence as adapter
import digital_souls_memory_hindsight.evidence.service as service

class Clock:
    current = datetime(2026, 10, 28, tzinfo=UTC)
    def __call__(self):
        return self.current

adapter.PostgresEvidenceStore = lambda url: MemoryStore()
real_service = service.EvidenceService
clock = Clock()
def configured_service(*args, **kwargs):
    return real_service(*args, clock=clock, **kwargs)
service.EvidenceService = configured_service
from digital_souls_memory_hindsight.app import app, ledger_service
from digital_souls_memory_hindsight.worker import Worker

async def idle_run(self):
    return None

# The test drives expiry itself; the lifespan worker would race it.
Worker.run = idle_run
with TestClient(app) as client:
    assert client.post('/sessions', json=session_body()).status_code == 201
    response = client.post('/turns', json=turn_body(perceived_by=['char-a', 'char-b']))
    assert response.status_code == 201, response.text
    assert ledger_service.get_projection_text('t-1', 'char-b') is None
    clock.current += timedelta(seconds=41)
    assert tuple(ledger_service.confirm_expired_deliveries()) == ()
    clock.current += timedelta(seconds=1)
    assert tuple(ledger_service.confirm_expired_deliveries()) == ('t-1',)
    assert ledger_service.get_projection_text('t-1', 'char-b') == response.json()['text']
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
            "DATABASE_URL": "postgresql://it1@postgres:5432/synthetic_ledger",
            "EVIDENCE_CHARACTER_IDS": '["char-a","char-b"]',
            "EVIDENCE_DELIVERY_TIMEOUT_SECONDS": "42",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
