"""Delivery decisions and projection rules, independent of storage and transport."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .domain import EvidenceValidationError, Session, Turn

DEFAULT_DELIVERY_TIMEOUT_SECONDS = 600
DeliveryStatus = Literal["immediate", "pending", "receipt_confirmed", "assumed_confirmed"]


@dataclass(frozen=True)
class DeliveryScope:
    turn_id: str
    status: DeliveryStatus
    spoken_until: int | None
    decided_at: datetime
    scope_id: int | None = None


def initial_delivery_scope(turn: Turn, session: Session) -> DeliveryScope | None:
    if turn.kind == "thought":
        return None
    # Saved self-perception identifies a character independently of registry changes.
    pending = (
        session.reports_delivery and turn.kind == "utterance" and turn.actor in turn.perceived_by
    )
    return DeliveryScope(
        turn.turn_id,
        "pending" if pending else "immediate",
        None if pending else len(turn.text),
        turn.recorded_at,
    )


def projection_text(turn: Turn, character_id: str, scope: DeliveryScope | None) -> str | None:
    if character_id not in turn.perceived_by:
        return None
    if character_id == turn.actor:
        return turn.text
    if scope is None:
        raise EvidenceValidationError("Delivery scope is missing")
    if scope.spoken_until is None:
        return None
    return turn.text[: scope.spoken_until]
