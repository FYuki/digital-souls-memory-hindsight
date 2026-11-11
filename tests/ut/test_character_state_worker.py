"""Gateway failures have bounded recovery and never save invalid results."""

import asyncio
import importlib
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from digital_souls_memory_hindsight.character_state.generator import CharacterStateGenerator
from digital_souls_memory_hindsight.character_state.ports import StateJob
from digital_souls_memory_hindsight.evidence.delivery import DeliveryScope
from digital_souls_memory_hindsight.evidence.domain import Session, StateKey
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.worker import Worker
from tests.ut.test_character_state_domain import source
from tests.ut.test_delivery_scope import NOW, Clock
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("attempt", [1, 2, 3])
@pytest.mark.parametrize("error", [RuntimeError, TimeoutError])
def test_transient_failure_defers_then_stops_after_three_attempts(
    attempt: int, error: type[Exception]
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.character_state.generator")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    ports = importlib.import_module("digital_souls_memory_hindsight.character_state.ports")
    job = ports.StateJob(
        key=StateKey("char-a", "s-1", "Web"),
        generation=2,
        attempts=attempt,
        lease_token="synthetic-lease",
        lease_until=NOW + timedelta(seconds=60),
    )
    store, evidence, deletion = MagicMock(), MagicMock(), MagicMock()
    store.claim.return_value = job
    evidence.get_session.return_value = Session("s-1", NOW, None, "Web", True)
    evidence.state_input_sources.return_value = (source("t-1", 0, "架空のお茶"),)
    deletion.turns_under_deletion.return_value = frozenset()
    gateway = MagicMock()
    gateway.generate_state = AsyncMock(side_effect=error("synthetic private content"))
    service_module = importlib.import_module(
        "digital_souls_memory_hindsight.character_state.service"
    )
    old = domain.CharacterState("穏やか", "温かい", "お茶")
    previous = ports.StoredState(1, 1, old, NOW)
    store.get.return_value = previous
    deletion.blocked_sessions.return_value = frozenset()
    service = service_module.CharacterStateService(store, evidence, deletion, clock=Clock())
    assert service.get("char-a", "s-1", "Web") == old
    store.get.return_value = replace(previous, generation=2)
    generator = module.CharacterStateGenerator(store, evidence, deletion, gateway, clock=Clock())
    asyncio.run(generator.run_once())
    assert service.get("char-a", "s-1", "Web") is None
    store.save.assert_not_called()
    if attempt < 3:
        store.defer.assert_called_once_with(
            job, NOW, NOW + timedelta(seconds=2 ** (attempt - 1)), "gateway_unavailable"
        )
        store.fail.assert_not_called()
    else:
        store.fail.assert_called_once_with(job, NOW, "gateway_unavailable")
        store.defer.assert_not_called()


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"mood": 1},
        {"mood": "", "relationship_temperature": "温かい", "current_topic": "お茶"},
        {"mood": "穏やか", "relationship_temperature": " \t ", "current_topic": "お茶"},
        {"mood": "穏やか", "relationship_temperature": "温かい", "current_topic": ""},
        {
            "mood": "x" * 201,
            "relationship_temperature": "温かい",
            "current_topic": "お茶",
        },
    ],
)
def test_invalid_output_is_terminal_without_partial_save(raw: object) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.character_state.generator")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    ports = importlib.import_module("digital_souls_memory_hindsight.character_state.ports")
    job = ports.StateJob(
        key=StateKey("char-a", "s-1", "Web"),
        generation=2,
        attempts=1,
        lease_token="synthetic-lease",
        lease_until=NOW + timedelta(seconds=60),
    )
    store, evidence, deletion, gateway = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    store.claim.side_effect = [job, None]
    evidence.get_session.return_value = Session("s-1", NOW, None, "Web", True)
    evidence.state_input_sources.return_value = (source("t-1", 0, "架空のお茶"),)
    deletion.turns_under_deletion.return_value = frozenset()
    gateway.generate_state = AsyncMock(return_value=raw)
    service_module = importlib.import_module(
        "digital_souls_memory_hindsight.character_state.service"
    )
    old = domain.CharacterState("穏やか", "温かい", "お茶")
    previous = ports.StoredState(1, 1, old, NOW)
    store.get.return_value = previous
    deletion.blocked_sessions.return_value = frozenset()
    service = service_module.CharacterStateService(store, evidence, deletion, clock=Clock())
    assert service.get("char-a", "s-1", "Web") == old
    store.get.return_value = replace(previous, generation=2)
    generator = module.CharacterStateGenerator(store, evidence, deletion, gateway, clock=Clock())
    asyncio.run(generator.run_once())
    asyncio.run(generator.run_once())
    store.fail.assert_called_once_with(job, NOW, "invalid_output")
    store.defer.assert_not_called()
    store.save.assert_not_called()
    assert gateway.generate_state.await_count == 1
    assert service.get("char-a", "s-1", "Web") is None


@pytest.mark.parametrize("condition", ["ended", "empty", "deleting"])
def test_ineligible_inputs_do_not_call_gateway(condition: str) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.character_state.generator")
    ports = importlib.import_module("digital_souls_memory_hindsight.character_state.ports")
    job = ports.StateJob(
        key=StateKey("char-a", "s-1", "Web"),
        generation=2,
        attempts=1,
        lease_token="synthetic-lease",
        lease_until=NOW + timedelta(seconds=60),
    )
    store, evidence, deletion, gateway = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    store.claim.return_value = job
    evidence.get_session.return_value = Session(
        "s-1", NOW, NOW if condition == "ended" else None, "Web", True
    )
    evidence.state_input_sources.return_value = (
        () if condition == "empty" else (source("t-1", 0, "架空のお茶"),)
    )
    deletion.turns_under_deletion.return_value = (
        frozenset({"t-1"}) if condition == "deleting" else frozenset()
    )
    gateway.generate_state = AsyncMock()
    generator = module.CharacterStateGenerator(store, evidence, deletion, gateway, clock=Clock())
    asyncio.run(generator.run_once())
    gateway.generate_state.assert_not_called()
    store.save.assert_not_called()
    store.complete.assert_called_once_with(job, NOW)


def test_existing_worker_reaches_state_generation_after_expiry() -> None:
    evidence, projection, state = MagicMock(), MagicMock(), MagicMock()
    projection.claim.return_value = None
    state.run_once = AsyncMock()
    worker = Worker(projection, None, evidence, clock=Clock(), state=state)
    asyncio.run(worker.run_once())
    evidence.confirm_expired_deliveries.assert_called_once()
    state.run_once.assert_awaited_once()


@pytest.mark.parametrize(
    "count,pending,max_chars,deleting,pages,references,selected,character",
    [
        (100, 0, 4000, False, 1, 20, 20, "char-b"),
        (21, 0, 4000, False, 1, 20, 20, "char-b"),
        (20, 0, 4000, False, 1, 20, 20, "char-b"),
        (50, 0, 4, False, 1, 3, 2, "char-b"),
        (50, 0, 4000, False, 1, 20, 20, "char-b"),
        (50, 25, 4000, False, 3, 45, 20, "char-b"),
        (50, 0, 4000, True, 2, 21, 20, "char-b"),
        (100, 0, 4000, False, 1, 20, 20, "char-a"),
    ],
)
def test_generation_stops_loading_once_input_window_is_decided(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    pending: int,
    max_chars: int,
    deleting: bool,
    pages: int,
    references: int,
    selected: int,
    character: str,
) -> None:
    ledger = MemoryStore()
    ledger.sessions["s-1"] = Session("s-1", NOW, None, "Web", True)
    for i in range(count):
        turn = replace(source(f"t-{i:03}", i, "茶🍵")[0], text="茶🍵")
        ledger.turns[turn.turn_id] = turn
        ledger.delivery_scopes.append(
            DeliveryScope(
                turn.turn_id,
                "pending" if i >= count - pending else "receipt_confirmed",
                None if i >= count - pending else 2,
                NOW,
            )
        )
    read = MagicMock(wraps=ledger.list_state_turns)
    scopes = MagicMock(wraps=ledger.get_delivery_scope)
    reread = MagicMock(wraps=ledger.get_turn)
    monkeypatch.setattr(ledger, "list_state_turns", read)
    monkeypatch.setattr(ledger, "get_delivery_scope", scopes)
    monkeypatch.setattr(ledger, "get_turn", reread)
    evidence = EvidenceService(ledger, frozenset({"char-a", "char-b"}), clock=Clock())
    store, deletion, gateway = MagicMock(), MagicMock(), MagicMock()
    latest = f"t-{count - 1:03}"
    deletion.turns_under_deletion.side_effect = lambda ids: (
        ids if deleting and latest in ids else frozenset()
    )
    store.claim.return_value = StateJob(
        StateKey(character, "s-1", "Web"), 1, 1, "synthetic-lease", NOW + timedelta(seconds=60)
    )
    gateway.generate_state = AsyncMock(
        return_value={
            "mood": "穏やか",
            "relationship_temperature": "温かい",
            "current_topic": "お茶",
        }
    )
    generator = CharacterStateGenerator(
        store, evidence, deletion, gateway, clock=Clock(), input_max_chars=max_chars
    )
    asyncio.run(generator.run_once())
    upper = count - pending - int(deleting)
    assert [t.turn_id for t in gateway.generate_state.call_args.args[3]] == [
        f"t-{i:03}" for i in range(upper - selected, upper)
    ]
    assert read.call_count == pages
    assert all(call.args[4] == 20 for call in read.call_args_list)
    assert scopes.call_count == (references if character == "char-b" else 0)
    assert deletion.turns_under_deletion.call_count == references - pending
    assert all(len(call.args[0]) == 1 for call in deletion.turns_under_deletion.call_args_list)
    reread.assert_not_called()
