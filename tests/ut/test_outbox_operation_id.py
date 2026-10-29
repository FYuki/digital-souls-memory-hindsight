"""Operation identity must survive retries without suppressing other targets."""

import importlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from digital_souls_memory_hindsight.outbox import RetainOperation

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
