"""Operation identity must survive retries without suppressing other targets."""

import importlib

import pytest

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
