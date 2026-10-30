"""Deletion persistence failures must not become successful acceptance or unblock recall."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.deletion.domain import (
    DeletionItem,
    DeletionLeaseLost,
    DeletionRequest,
    transition,
)
from digital_souls_memory_hindsight.deletion.ports import DeletionTransitionRejected
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


def request() -> DeletionRequest:
    return DeletionRequest(
        request_id="d",
        requested_by="synthetic-author",
        target_turn_ids=("t-1",),
        status="accepted",
        cascade_result=None,
        error=None,
        requested_at=NOW,
        completed_at=None,
        user_id=None,
    )


@pytest.mark.parametrize("operation", ["progress", "ledger", "provisions"])
def test_expired_or_replaced_deletion_lease_prevents_writes(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    item = DeletionItem("d", "processing", 1, NOW, "old-lease", NOW, None, None, {})
    store = PostgresDeletionStore("synthetic-db")
    with pytest.raises(DeletionLeaseLost):
        if operation == "progress":
            store.save_progress(item, {"ledger_done": True}, NOW)
        elif operation == "ledger":
            store.delete_turns(item, request(), NOW)
        else:
            store.delete_provisions_for_memories(item, "char-a", frozenset({"m"}), NOW)
    connection.execute.assert_called_once()
    assert connection.execute.call_args.args[1] == ("d", "old-lease", NOW)
    assert connection.__exit__.call_args.args[0] is DeletionLeaseLost


@pytest.mark.parametrize("matched", [True, False])
@pytest.mark.parametrize("finish", ["complete", "defer", "fail"])
def test_deletion_queue_finish_is_fenced_by_current_lease(
    monkeypatch: pytest.MonkeyPatch, matched: bool, finish: str
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = ("d",) if matched else None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    item = DeletionItem(
        "d", "processing", 1, NOW, "lease", NOW + timedelta(seconds=60), None, None, {}
    )
    store = PostgresDeletionStore("synthetic-db")
    if finish == "complete":
        result = store.complete(item, NOW)
    elif finish == "defer":
        result = store.defer(item, NOW, NOW + timedelta(seconds=1), "pending")
    else:
        result = store.fail(item, NOW, "rebuild_failed")
    assert result == matched
    sql, parameters = connection.execute.call_args.args
    assert parameters[-3:] == ("d", "lease", NOW)
    assert "lease_until > %s" in sql and "status = 'processing'" in sql


@pytest.mark.parametrize("seconds", [0, -1])
def test_deletion_queue_rejects_nonpositive_lease_before_connecting(
    monkeypatch: pytest.MonkeyPatch, seconds: int
) -> None:
    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(ValueError):
        PostgresDeletionStore("synthetic-db").claim(NOW, timedelta(seconds=seconds))
    connect.assert_not_called()


def test_deletion_claim_returns_saved_progress_and_can_reclaim_expired_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = DeletionItem(
        "d",
        "processing",
        2,
        NOW,
        "new-lease",
        NOW + timedelta(seconds=60),
        None,
        None,
        {"memory_snapshot": {"char-a": ["m"]}},
    )
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = item
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    assert PostgresDeletionStore("synthetic-db").claim(NOW, timedelta(seconds=60)) == item
    sql, parameters = cursor.execute.call_args.args
    assert "lease_until <= %s" in sql
    assert parameters[:2] == (NOW, NOW)
    assert parameters[-1] == NOW + timedelta(seconds=60)


def test_add_returns_existing_request_without_adding_new_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = request()
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = previous
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    actual = PostgresDeletionStore("synthetic-db").add(previous, frozenset({("char-a", "s-1")}))
    assert actual == previous
    assert not any(
        "INSERT INTO deletion_blocks" in call.args[0] for call in connection.execute.call_args_list
    )
    assert not any(
        "INSERT INTO deletion_outbox" in call.args[0] for call in connection.execute.call_args_list
    )


def test_new_acceptance_enqueues_deletion_on_the_same_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = ("d",)
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    saved = PostgresDeletionStore("synthetic-db").add(
        request(), frozenset({("char-a", "s-1"), ("char-b", "s-1")})
    )
    assert saved == request()
    connect.assert_called_once()
    enqueues = [
        call
        for call in connection.execute.call_args_list
        if "INSERT INTO deletion_outbox" in call.args[0]
    ]
    assert len(enqueues) == 1
    assert "d" in enqueues[0].args[1]


def test_outbox_insert_failure_does_not_report_accepted_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    result = MagicMock()
    result.fetchone.return_value = ("d",)

    def execute(sql: str, parameters: object) -> MagicMock:
        if "INSERT INTO deletion_outbox" in sql:
            raise psycopg.OperationalError("synthetic enqueue failure")
        return result

    connection.execute.side_effect = execute
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    with pytest.raises(EvidenceUnavailable):
        PostgresDeletionStore("synthetic-db").add(request(), frozenset({("char-a", "s-1")}))
    assert connection.__exit__.call_args.args[0] is psycopg.OperationalError


@pytest.mark.parametrize("stage", ["connect", "request", "blocks", "commit"])
def test_acceptance_storage_failure_does_not_return_success(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = ("d",)
    if stage == "request":
        connection.execute.side_effect = psycopg.OperationalError("synthetic insert failure")
    if stage == "blocks":
        connection.execute.side_effect = [
            connection.execute.return_value,
            psycopg.OperationalError("synthetic block failure"),
        ]

    @contextmanager
    def connect(
        url: str, *, connect_timeout: int
    ) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        if stage == "connect":
            raise psycopg.OperationalError("synthetic connect failure")
        yield cast(psycopg.Connection[tuple[object, ...]], connection)
        if stage == "commit":
            raise psycopg.OperationalError("synthetic commit failure")

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(EvidenceUnavailable):
        PostgresDeletionStore("synthetic-db").add(request(), frozenset({("char-a", "s-1")}))


@pytest.mark.parametrize("matched", [False, True])
def test_transition_requires_matching_previous_status(
    monkeypatch: pytest.MonkeyPatch, matched: bool
) -> None:
    updated = transition(request(), "processing", NOW)
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = ("d",) if matched else None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    store = PostgresDeletionStore("synthetic-db")
    if matched:
        assert store.save_transition("accepted", updated) == updated
    else:
        with pytest.raises(DeletionTransitionRejected):
            store.save_transition("accepted", updated)
    assert connection.execute.call_args.args[1][-2:] == ("d", "accepted")


def test_store_rejects_transition_that_skips_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(DeletionTransitionRejected):
        PostgresDeletionStore("synthetic-db").save_transition(
            "accepted", replace(request(), status="completed", completed_at=NOW)
        )
    connect.assert_not_called()


@pytest.mark.parametrize("operation", ["get", "blocked_sessions"])
@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
def test_database_failure_is_unavailable_instead_of_empty_result(
    monkeypatch: pytest.MonkeyPatch, operation: str, stage: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_deletion")
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = None
    connection.execute.return_value.fetchall.return_value = []
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    if stage == "execute":
        connection.execute.side_effect = psycopg.OperationalError("synthetic execute failure")
        cursor.execute.side_effect = psycopg.OperationalError("synthetic cursor failure")

    @contextmanager
    def connect(
        url: str, *, connect_timeout: int
    ) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        if stage == "connect":
            raise psycopg.OperationalError("synthetic connection failure")
        yield cast(psycopg.Connection[tuple[object, ...]], connection)
        if stage == "commit":
            raise psycopg.OperationalError("synthetic commit failure")

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(EvidenceUnavailable):
        getattr(module.PostgresDeletionStore("synthetic-db"), operation)("synthetic-id")
