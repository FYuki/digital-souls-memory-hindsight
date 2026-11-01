"""Storage failures do not masquerade as empty queues or successful completion."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.outbox import OutboxItem
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("method", ["get", "claim", "complete", "defer", "fail", "owns"])
def test_database_errors_escape_as_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    def unavailable(url: str, *, connect_timeout: int) -> None:
        raise psycopg.OperationalError("synthetic private detail")

    monkeypatch.setattr(psycopg, "connect", unavailable)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    queue = PostgresOutbox("synthetic")
    item = OutboxItem(
        "turn",
        "bank",
        "session",
        "架空のお茶",
        now,
        "processing",
        1,
        now,
        "lease",
        now + timedelta(seconds=60),
        None,
        None,
    )
    with pytest.raises(EvidenceUnavailable):
        if method == "get":
            queue.get(item.operation.operation_id)
        elif method == "claim":
            queue.claim(now, timedelta(seconds=60))
        elif method == "complete":
            queue.complete(item, now)
        elif method == "defer":
            queue.defer(item, now, now + timedelta(seconds=1), "transport_error")
        elif method == "owns":
            queue.owns(item, now)
        else:
            queue.fail(item, now, "retain_failed")


@pytest.mark.parametrize("matched", [False, True])
def test_ownership_uses_the_operation_and_current_lease(
    monkeypatch: pytest.MonkeyPatch,
    matched: bool,
) -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    item = OutboxItem(
        "turn",
        "bank",
        "session",
        "合成本文",
        now,
        "processing",
        1,
        now,
        "lease",
        now + timedelta(seconds=60),
        None,
        None,
    )
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (
        (item.operation.operation_id,) if matched else None
    )
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    assert PostgresOutbox("synthetic").owns(item, now) == matched
    assert connection.execute.call_args.args[1] == (item.operation.operation_id, "lease", now)


@pytest.mark.parametrize("seconds", [0, -1])
def test_nonpositive_lease_is_rejected_before_accessing_storage(seconds: int) -> None:
    with pytest.raises(ValueError):
        PostgresOutbox("synthetic").claim(
            datetime(2026, 10, 28, tzinfo=UTC),
            timedelta(seconds=seconds),
        )
