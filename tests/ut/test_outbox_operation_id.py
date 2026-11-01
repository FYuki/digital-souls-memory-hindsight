"""Operation identity must survive retries without suppressing other targets."""

import importlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.outbox import RetainOperation
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


def test_operation_identifier_is_stable_for_the_same_turn_and_target() -> None:
    outbox = importlib.import_module("digital_souls_memory_hindsight.outbox")
    first = outbox.operation_id("turn-1", "bank-a", "session-1")
    second = outbox.operation_id("turn-1", "bank-a", "session-1")
    assert first == second


@pytest.mark.parametrize(
    "other",
    [
        ("turn-2", "bank-a", "session-1"),
        ("turn-1", "bank-b", "session-1"),
        ("turn-1", "bank-a", "session-2"),
    ],
)
def test_operation_identifier_distinguishes_turn_and_projection_target(
    other: tuple[str, str, str],
) -> None:
    outbox = importlib.import_module("digital_souls_memory_hindsight.outbox")
    assert outbox.operation_id("turn-1", "bank-a", "session-1") != outbox.operation_id(*other)


@pytest.mark.parametrize("separator", [":", "/", "|"])
def test_operation_identifier_does_not_confuse_component_boundaries(separator: str) -> None:
    outbox = importlib.import_module("digital_souls_memory_hindsight.outbox")
    first = outbox.operation_id(f"turn{separator}part", "bank", "session")
    second = outbox.operation_id("turn", f"part{separator}bank", "session")
    assert first != second


def test_rebuild_and_delete_identifiers_distinguish_mode_and_saved_scope() -> None:
    append = RetainOperation("turn", "bank", "session", "本文", datetime(2020, 1, 1, tzinfo=UTC))
    rebuild = replace(append, update_mode="replace", source_scope_id=7)
    deleted = replace(append, update_mode="delete", source_scope_id=8, content="")
    assert (
        len(
            {
                append.operation_id,
                rebuild.operation_id,
                deleted.operation_id,
                replace(deleted, source_scope_id=7).operation_id,
            }
        )
        == 4
    )
    assert replace(deleted).operation_id == deleted.operation_id


def test_delete_identifier_requires_a_saved_delivery_scope() -> None:
    operation = RetainOperation(
        "turn", "bank", "session", "", datetime(2020, 1, 1, tzinfo=UTC), "delete"
    )
    with pytest.raises(ValueError):
        _ = operation.operation_id


def deletion_operation(**changes: object) -> RetainOperation:
    outbox = importlib.import_module("digital_souls_memory_hindsight.outbox")
    arguments = {
        "turn_id": None,
        "bank_id": "bank",
        "document_id": "session",
        "content": "合成の残る本文",
        "occurred_at": datetime(2020, 1, 1, tzinfo=UTC),
        "update_mode": "replace",
        "deletion_request_id": "7",
        **changes,
    }
    return cast(
        RetainOperation,
        outbox.RetainOperation(**arguments),
    )


def test_deletion_rebuild_resubmission_keeps_one_operation_with_same_payload() -> None:
    first = deletion_operation()
    connection = MagicMock(spec=psycopg.Connection)
    inserted, duplicate, restored = MagicMock(), MagicMock(), MagicMock()
    inserted.fetchone.return_value = (first.operation_id,)
    duplicate.fetchone.return_value = None
    restored.fetchone.return_value = (
        first.turn_id,
        first.bank_id,
        first.document_id,
        first.content,
        first.occurred_at,
        first.update_mode,
        first.source_scope_id,
        "7",
    )
    connection.execute.side_effect = [inserted, duplicate, restored]
    store = PostgresEvidenceStore("synthetic-db", connection)
    store.add_retain(first)
    store.add_retain(deletion_operation())
    initial, repeated, read = connection.execute.call_args_list
    assert initial.args == repeated.args
    assert "ON CONFLICT (operation_id) DO NOTHING" in initial.args[0]
    assert "deletion_request_id" in initial.args[0]
    assert initial.args[1][-1] == "7"
    assert "deletion_request_id" in read.args[0]
    assert read.args[1] == (first.operation_id,)


def test_deletion_rebuild_and_delivery_scope_rebuild_can_both_be_enqueued() -> None:
    deletion = deletion_operation()
    delivery = RetainOperation(
        "turn", "bank", "session", deletion.content, deletion.occurred_at, "replace", 7
    )
    store = MemoryStore()
    store.add_retain(delivery)
    store.add_retain(deletion)
    assert deletion.operation_id != delivery.operation_id
    assert set(store.outbox) == {delivery.operation_id, deletion.operation_id}


@pytest.mark.parametrize(
    "changes",
    [
        {"deletion_request_id": "other"},
        {"bank_id": "other"},
        {"document_id": "other"},
        {"update_mode": "delete"},
    ],
)
def test_deletion_rebuild_identifier_distinguishes_request_target_and_mode(
    changes: dict[str, object],
) -> None:
    first = deletion_operation()
    other = deletion_operation(**changes)
    assert first.operation_id != other.operation_id


@pytest.mark.parametrize("mode", ["replace", "delete"])
def test_saved_outbox_restores_deletion_identity(mode: Literal["replace", "delete"]) -> None:
    outbox = importlib.import_module("digital_souls_memory_hindsight.outbox")
    item = outbox.OutboxItem(
        turn_id=None,
        bank_id="bank",
        document_id="session",
        content="合成の本文" if mode == "replace" else "",
        occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
        status="processing",
        attempts=2,
        available_at=datetime(2020, 1, 1, tzinfo=UTC),
        lease_token="synthetic-lease",
        lease_until=datetime(2020, 1, 2, tzinfo=UTC),
        last_error=None,
        completed_at=None,
        update_mode=mode,
        deletion_request_id="7",
    )
    expected = replace(deletion_operation(), content=item.content, update_mode=mode)
    assert item.operation == expected
    assert item.operation.operation_id == expected.operation_id
