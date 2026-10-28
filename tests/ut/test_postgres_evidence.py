"""DB failures must escape the persistence boundary as sanitized evidence errors."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.delivery import DeliveryScope
from digital_souls_memory_hindsight.evidence.domain import EvidenceValidationError, Session
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore

pytestmark = pytest.mark.ut


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
    "operation", ["write", "read", "lock", "pending", "session-lock", "projection-turns"]
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
        else:
            store.list_pending_deliveries(now)
