"""Evidence mutations notify the state owner; delivery rules remain shared."""

import importlib
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from tests.ut.test_delivery_scope import NOW, Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


def test_new_turn_and_each_receipt_advance_only_affected_state_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.evidence.domain")
    ledger, clock = MemoryStore(), Clock()
    advance = MagicMock(wraps=ledger.advance_character_states)
    monkeypatch.setattr(ledger, "advance_character_states", advance, raising=False)
    service = service_with_session(ledger, clock)
    service.add_turn(draft())
    keys = frozenset(
        {domain.StateKey("char-a", "s-1", "Web"), domain.StateKey("char-b", "s-1", "Web")}
    )
    advance.assert_called_once_with(keys, NOW)
    advance.reset_mock()
    service.add_turn(draft())
    advance.assert_not_called()
    for status, end in (("partial", 2), ("none", 0), ("none", 0)):
        service.add_receipt(DeliveryReceipt("t-1", status, end, "Web", NOW))
    assert advance.call_count == 3
    assert all(call.args == (keys, NOW) for call in advance.call_args_list)
    assert ledger.state_generations == dict.fromkeys(keys, 4)
    assert ledger.state_pending == dict.fromkeys(keys, NOW)


def test_assumed_confirmation_advances_once_and_ending_clears_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, clock = MemoryStore(), Clock()
    advance = MagicMock(wraps=ledger.advance_character_states)
    clear = MagicMock(wraps=ledger.clear_character_states)
    monkeypatch.setattr(ledger, "advance_character_states", advance, raising=False)
    monkeypatch.setattr(ledger, "clear_character_states", clear, raising=False)
    service = service_with_session(ledger, clock)
    service.add_turn(draft())
    advance.reset_mock()
    clock.now += timedelta(seconds=600)
    service.confirm_expired_deliveries()
    assert advance.call_count == 1
    service.confirm_expired_deliveries()
    assert advance.call_count == 1
    service.add_ending("s-1", clock.now, "Web")
    clear.assert_called_once_with("s-1")
    assert set(ledger.state_generations.values()) == {3}
    assert ledger.state_pending == {}
    service.add_ending("s-1", clock.now, "Web")
    clear.assert_called_once_with("s-1")


def test_state_sources_share_perception_and_delivered_range_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = MemoryStore()
    monkeypatch.setattr(ledger, "advance_character_states", MagicMock(), raising=False)
    service = service_with_session(ledger, Clock())
    turn = service.add_turn(draft())
    candidates = MagicMock(return_value=(turn,))
    monkeypatch.setattr(ledger, "list_state_turns", candidates, raising=False)
    read = service.state_input_sources
    assert tuple(read("char-b", "s-1", "Web", 20)) == ()
    service.add_receipt(DeliveryReceipt("t-1", "partial", 3, "Web", NOW))
    assert tuple(read("char-b", "s-1", "Web", 20)) == ((turn, turn.text[:3]),)
    assert tuple(read("char-a", "s-1", "Web", 20)) == ((turn, turn.text),)
    assert tuple(read("char-c", "s-1", "Web", 20)) == ()
    service.add_receipt(DeliveryReceipt("t-1", "none", 0, "Web", NOW))
    assert tuple(read("char-b", "s-1", "Web", 20)) == ()
    assert candidates.call_args.args == ("char-b", "s-1", "Web", None, 20)


def test_state_keys_use_turn_surface_and_perceivers() -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.evidence.domain")
    turn = replace(draft(), surface="App", perceived_by=("char-a",))
    from digital_souls_memory_hindsight.evidence.domain import prepare_turn
    from tests.ut.test_delivery_scope import CHARACTERS

    saved = prepare_turn(turn, CHARACTERS, NOW)
    assert domain.state_keys((saved, saved)) == frozenset({domain.StateKey("char-a", "s-1", "App")})


def test_state_sources_report_missing_delivery_without_rereading_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = MemoryStore()
    service = service_with_session(ledger, Clock())
    service.add_turn(draft())
    ledger.delivery_scopes.clear()
    reread = MagicMock(wraps=ledger.get_turn)
    monkeypatch.setattr(ledger, "get_turn", reread)
    with pytest.raises(EvidenceUnavailable):
        tuple(service.state_input_sources("char-b", "s-1", "Web", 20))
    reread.assert_not_called()


def test_private_thought_source_needs_no_delivery_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = MemoryStore()
    service = service_with_session(ledger, Clock())
    thought = service.add_turn(replace(draft(), kind="thought", perceived_by=("char-a",)))
    scopes = MagicMock(wraps=ledger.get_delivery_scope)
    monkeypatch.setattr(ledger, "get_delivery_scope", scopes)
    assert tuple(service.state_input_sources("char-a", "s-1", "Web", 20)) == (
        (thought, thought.text),
    )
    scopes.assert_not_called()
