"""Reads recheck the same stored state after generation and visibility changes."""

import importlib
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from digital_souls_memory_hindsight.evidence.domain import Session, StateKey
from digital_souls_memory_hindsight.evidence.ports import EvidenceNotFound, EvidenceUnavailable
from tests.ut.test_delivery_scope import NOW, Clock

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("change", ["generation", "deletion", "ending", "expiry"])
def test_same_service_stops_returning_previously_generated_state(change: str) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.character_state.service")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    ports = importlib.import_module("digital_souls_memory_hindsight.character_state.ports")
    clock, store, evidence, deletion = Clock(), MagicMock(), MagicMock(), MagicMock()
    state = domain.CharacterState("穏やか", "温かい", "お茶")
    row = ports.StoredState(generation=1, state_generation=1, state=state, generated_at=NOW)
    store.get.return_value = row
    session = Session("s-1", NOW, None, "Web", True)
    evidence.get_session.return_value = session
    deletion.blocked_sessions.return_value = frozenset()
    service = module.CharacterStateService(store, evidence, deletion, clock=clock)
    assert service.get("char-a", "s-1", "Web") == state
    if change == "generation":
        store.get.return_value = replace(row, generation=2)
    elif change == "deletion":
        deletion.blocked_sessions.return_value = frozenset({"s-1"})
    elif change == "ending":
        evidence.get_session.return_value = replace(session, ended_at=NOW)
    else:
        clock.now += timedelta(seconds=1801)
    assert service.get("char-a", "s-1", "Web") is None


@pytest.mark.parametrize("missing_row", [True, False])
def test_ungenerated_state_is_null(missing_row: bool) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.character_state.service")
    ports = importlib.import_module("digital_souls_memory_hindsight.character_state.ports")
    store, evidence, deletion = MagicMock(), MagicMock(), MagicMock()
    store.get.return_value = None if missing_row else ports.StoredState(1, None, None, None)
    evidence.get_session.return_value = Session("s-1", NOW, None, "Web", True)
    deletion.blocked_sessions.return_value = frozenset()
    service = module.CharacterStateService(store, evidence, deletion, clock=Clock())
    assert service.get("char-a", "s-1", "Web") is None


def test_state_key_includes_character_session_and_surface() -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.character_state.service")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    ports = importlib.import_module("digital_souls_memory_hindsight.character_state.ports")
    key = StateKey("char-a", "s-1", "Web")
    state = domain.CharacterState("穏やか", "温かい", "お茶")
    row = ports.StoredState(1, 1, state, NOW)
    store, evidence, deletion = MagicMock(), MagicMock(), MagicMock()
    store.get.side_effect = {key: row}.get
    evidence.get_session.return_value = Session("s-1", NOW, None, "Web", True)
    deletion.blocked_sessions.return_value = frozenset()
    service = module.CharacterStateService(store, evidence, deletion, clock=Clock())
    assert service.get(*key) == state
    for other in (("char-b", "s-1", "Web"), ("char-a", "s-2", "Web"), ("char-a", "s-1", "App")):
        assert service.get(*other) is None


@pytest.mark.parametrize("error", [EvidenceNotFound, EvidenceUnavailable])
def test_dependency_failure_is_not_disguised_as_null(error: type[Exception]) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.character_state.service")
    evidence = MagicMock()
    evidence.get_session.side_effect = error()
    service = module.CharacterStateService(MagicMock(), evidence, MagicMock(), clock=Clock())
    with pytest.raises(error):
        service.get("char-a", "s-1", "Web")
