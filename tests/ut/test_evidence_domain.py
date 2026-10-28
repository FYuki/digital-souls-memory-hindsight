"""Direct observations of evidence values at the domain boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from digital_souls_memory_hindsight.evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
    TurnInput,
    matches_saved_turn,
    prepare_turn,
    validate_receipt,
)

pytestmark = pytest.mark.ut
NOW = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.mark.parametrize(
    ("actor", "perceived_by", "saved_mode", "requested_mode", "expected"),
    [
        ("char-a", ("char-a",), "broadcast", "broadcast", True),
        ("char-a", ("char-a",), "broadcast", None, False),
        ("user-a", (), "broadcast", None, True),
        ("user-a", (), "self", None, False),
        ("user-a", (), "direct", None, False),
    ],
)
def test_saved_turn_comparison_limits_implicit_mode_to_user_broadcast(
    actor: str,
    perceived_by: tuple[str, ...],
    saved_mode: str,
    requested_mode: str | None,
    expected: bool,
) -> None:
    value = replace(
        draft({}),
        actor=actor,
        perceived_by=perceived_by,
        address_mode=saved_mode,
        addressees=("user-b",) if saved_mode == "direct" else (),
    )
    saved = prepare_turn(value, frozenset({"char-a"}), NOW)
    request = replace(value, address_mode=requested_mode)
    assert matches_saved_turn(request, saved) is expected
    assert matches_saved_turn(replace(saved, recorded_at=NOW + timedelta(seconds=1)), saved)


def draft(register: dict[str, str]) -> TurnInput:
    return TurnInput(
        "t-1",
        "s-1",
        "Web",
        "char-a",
        "utterance",
        " 茶🍵。\n",
        ("char-a",),
        (),
        "self",
        None,
        "ja",
        register,
        "public",
        "fixture-1",
        NOW,
    )


def test_prepared_turn_retains_original_input_when_caller_changes_register() -> None:
    original = {"一人称": "私"}
    value = prepare_turn(draft(original), frozenset({"char-a"}), NOW)
    original["一人称"] = "僕"
    assert value.register == {"一人称": "私"}
    assert value.text == " 茶🍵。\n"
    assert value.address_mode == "self"
    assert value.recorded_at == NOW


def test_session_cannot_end_before_it_starts() -> None:
    with pytest.raises(EvidenceValidationError):
        Session("s-1", NOW, NOW - timedelta(seconds=1), "Web", False)


@pytest.mark.parametrize("kind", ["utterance", "observation", "thought"])
def test_receipt_eligibility_is_independent_of_address_mode(kind: str) -> None:
    turn = prepare_turn(replace(draft({}), kind=kind), frozenset({"char-a"}), NOW)
    receipt = DeliveryReceipt("t-1", "full", len(turn.text), "Web", NOW)
    session = Session("s-1", NOW, None, "Web", True)
    if kind == "thought":
        with pytest.raises(EvidenceValidationError):
            validate_receipt(receipt, turn, session)
    else:
        validate_receipt(receipt, turn, session)
