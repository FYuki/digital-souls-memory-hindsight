"""DB failures must escape the persistence boundary as sanitized evidence errors."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.delivery import DeliveryScope
from digital_souls_memory_hindsight.evidence.domain import (
    EvidenceValidationError,
    Session,
    StateKey,
)
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from tests.ut.test_character_state_domain import source
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


def test_state_generation_and_outbox_share_the_bound_connection_and_generation() -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = (3,)
    store = PostgresEvidenceStore("synthetic-db", connection)
    key = StateKey("char-a", "s-1", "Web")
    other = StateKey("char-b", "s-1", "App")
    store.advance_character_states(frozenset({other, key}), NOW)
    assert connection.execute.call_args_list[0].args[1] == key
    assert connection.execute.call_args_list[1].args[1] == (*key, 3, NOW)
    assert connection.execute.call_args_list[2].args[1] == other
    assert connection.execute.call_args_list[3].args[1] == (*other, 3, NOW)


def test_state_input_query_preserves_character_session_and_surface() -> None:
    connection = MagicMock(spec=psycopg.Connection)
    cursor = connection.cursor.return_value.__enter__.return_value
    turn, _ = source("synthetic-turn", 0, "架空のお茶")
    cursor.fetchall.return_value = [turn]
    store = PostgresEvidenceStore("synthetic-db", connection)
    assert store.list_state_turns("char-a", "s-1", "Web", None, 20) == (turn,)
    assert cursor.execute.call_args.args[1] == ("char-a", "s-1", "Web", 20)
    assert 'turn_id COLLATE "C" DESC LIMIT %s' in cursor.execute.call_args.args[0]
    before = (turn.occurred_at, turn.recorded_at, turn.turn_id)
    assert store.list_state_turns("char-a", "s-1", "Web", before, 7) == (turn,)
    query, parameters = cursor.execute.call_args.args
    assert parameters == ("char-a", "s-1", "Web", *before, 7)
    assert 'AND (occurred_at,recorded_at,turn_id COLLATE "C") < (%s,%s,%s)' in query


def test_projection_sessions_returns_rows_for_the_bound_bank() -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchall.return_value = [("s-1",), ("s-2",)]
    store = PostgresEvidenceStore("synthetic-db", connection)
    assert store.list_projection_sessions("requested-bank") == ("s-1", "s-2")
    query, parameters = connection.execute.call_args.args
    assert parameters == ("requested-bank",)
    assert "requested-bank" not in query


@pytest.mark.parametrize("error", [psycopg.OperationalError, psycopg.IntegrityError])
def test_database_failure_is_translated_at_storage_boundary(
    monkeypatch: pytest.MonkeyPatch, error: type[psycopg.Error]
) -> None:
    def connect(url: str, *, connect_timeout: int) -> None:
        assert url == "synthetic-db"
        assert connect_timeout == 5
        raise error("synthetic private connection detail")

    monkeypatch.setattr(psycopg, "connect", connect)
    store = PostgresEvidenceStore("synthetic-db")
    expected = EvidenceValidationError if error is psycopg.IntegrityError else EvidenceUnavailable
    with pytest.raises(expected):
        store.get_session("s-1")


def test_failed_commit_does_not_return_success(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = ("s-1",)

    @contextmanager
    def connect(
        url: str, *, connect_timeout: int
    ) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        assert url == "synthetic-db"
        assert connect_timeout == 5
        yield cast(psycopg.Connection[tuple[object, ...]], connection)
        raise psycopg.OperationalError("synthetic failed commit")

    monkeypatch.setattr(psycopg, "connect", connect)
    store = PostgresEvidenceStore("synthetic-db")
    session = Session("s-1", datetime(2026, 10, 28, tzinfo=UTC), None, "Web", False)
    with pytest.raises(EvidenceUnavailable):
        store.add_session(session)
    arguments = connection.execute.call_args.args
    assert arguments[1] == (session.session_id, session.started_at, None, "Web", False)
    assert session.session_id not in arguments[0]


@pytest.mark.parametrize(
    "operation",
    [
        "write",
        "read",
        "lock",
        "pending",
        "session-lock",
        "projection-turns",
        "projection-sessions",
        "turns-batch",
        "scopes-batch",
        "projection-turns-batch",
        "state-advance",
        "state-clear",
        "state-turns",
    ],
)
@pytest.mark.parametrize("error", [psycopg.OperationalError, psycopg.IntegrityError])
def test_delivery_storage_failures_preserve_error_translation(
    monkeypatch: pytest.MonkeyPatch, operation: str, error: type[psycopg.Error]
) -> None:
    def connect(url: str, *, connect_timeout: int) -> None:
        raise error("synthetic storage failure")

    monkeypatch.setattr(psycopg, "connect", connect)
    store = PostgresEvidenceStore("synthetic-db")
    now = datetime(2026, 10, 28, tzinfo=UTC)
    expected = EvidenceValidationError if error is psycopg.IntegrityError else EvidenceUnavailable
    with pytest.raises(expected):
        if operation == "write":
            store.add_delivery_scope(DeliveryScope("t-1", "pending", None, now))
        elif operation == "read":
            store.get_delivery_scope("t-1")
        elif operation == "lock":
            store.get_turn_for_update("t-1")
        elif operation == "session-lock":
            store.lock_session("s-1")
        elif operation == "projection-turns":
            store.list_projection_turns("s-1", "bank")
        elif operation == "projection-sessions":
            store.list_projection_sessions("bank")
        elif operation == "turns-batch":
            store.get_turns(frozenset({"t-1"}))
        elif operation == "scopes-batch":
            store.get_delivery_scopes(frozenset({"t-1"}))
        elif operation == "projection-turns-batch":
            store.list_projection_turns_for_sessions(frozenset({"s-1"}), "bank")
        elif operation == "state-advance":
            store.advance_character_states(frozenset({StateKey("char-a", "s-1", "Web")}), now)
        elif operation == "state-clear":
            store.clear_character_states("s-1")
        elif operation == "state-turns":
            store.list_state_turns("char-a", "s-1", "Web", None, 20)
        else:
            store.list_pending_deliveries(now)
