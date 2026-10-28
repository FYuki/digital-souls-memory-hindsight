"""Evidence values and registration rules, independent of HTTP and storage."""

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime
from types import MappingProxyType


class EvidenceValidationError(ValueError):
    """A registration violates an evidence invariant."""


@dataclass(frozen=True)
class Session:
    session_id: str
    started_at: datetime
    ended_at: datetime | None
    surface: str
    reports_delivery: bool

    def __post_init__(self) -> None:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise EvidenceValidationError("Session ends before it starts")


@dataclass(frozen=True)
class TurnInput:
    turn_id: str
    session_id: str
    surface: str
    actor: str
    kind: str
    text: str
    perceived_by: tuple[str, ...]
    addressees: tuple[str, ...]
    address_mode: str | None
    reply_to_turn_id: str | None
    language: str
    register: Mapping[str, str]
    sensitivity: str
    classifier_version: str
    occurred_at: datetime


@dataclass(frozen=True)
class Turn(TurnInput):
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "perceived_by", tuple(self.perceived_by))
        object.__setattr__(self, "addressees", tuple(self.addressees))
        object.__setattr__(self, "register", MappingProxyType(dict(self.register)))


@dataclass(frozen=True)
class DeliveryReceipt:
    turn_id: str
    status: str
    spoken_until: int
    reported_by: str
    reported_at: datetime


@dataclass(frozen=True)
class SessionEnding:
    session_id: str
    ended_at: datetime
    reported_by: str
    reported_at: datetime


def matches_saved_turn(value: TurnInput, saved: Turn) -> bool:
    # The saved perception invariant identifies the actor's role at registration,
    # independently of the current character registry.
    if value.address_mode is None and saved.actor not in saved.perceived_by:
        value = replace(value, address_mode="broadcast")
    return all(
        getattr(value, field.name) == getattr(saved, field.name) for field in fields(TurnInput)
    )


def prepare_turn(value: TurnInput, character_ids: frozenset[str], recorded_at: datetime) -> Turn:
    if value.kind not in {"utterance", "observation", "thought"}:
        raise EvidenceValidationError("Unknown turn kind")
    for identifiers in (value.perceived_by, value.addressees):
        if len(set(identifiers)) != len(identifiers) or any(not v.strip() for v in identifiers):
            raise EvidenceValidationError("Invalid participant list")
    is_character = value.actor in character_ids
    if not set(value.perceived_by) <= character_ids:
        raise EvidenceValidationError("Perception requires characters")
    if is_character and value.actor not in value.perceived_by:
        raise EvidenceValidationError("Character must perceive its own turn")
    if value.kind == "thought" and (not is_character or value.perceived_by != (value.actor,)):
        raise EvidenceValidationError("Thought is private to its character actor")
    mode = value.address_mode
    if mode is None:
        if is_character or value.addressees:
            raise EvidenceValidationError("Address mode is required")
        mode = "broadcast"
    if mode not in {"direct", "broadcast", "self"}:
        raise EvidenceValidationError("Unknown address mode")
    if (mode == "direct") != bool(value.addressees):
        raise EvidenceValidationError("Address mode and addressees disagree")
    return Turn(
        turn_id=value.turn_id,
        session_id=value.session_id,
        surface=value.surface,
        actor=value.actor,
        kind=value.kind,
        text=value.text,
        perceived_by=value.perceived_by,
        addressees=value.addressees,
        address_mode=mode,
        reply_to_turn_id=value.reply_to_turn_id,
        language=value.language,
        register=value.register,
        sensitivity=value.sensitivity,
        classifier_version=value.classifier_version,
        occurred_at=value.occurred_at,
        recorded_at=recorded_at,
    )


def validate_receipt(value: DeliveryReceipt, turn: Turn, session: Session) -> None:
    if not session.reports_delivery or turn.kind == "thought":
        raise EvidenceValidationError("Turn does not accept receipts")
    length = len(turn.text)
    if type(value.spoken_until) is not int or not (
        (value.status == "none" and value.spoken_until == 0)
        or (value.status == "full" and value.spoken_until == length)
        or (value.status == "partial" and 0 < value.spoken_until < length)
    ):
        raise EvidenceValidationError("Receipt range does not match text")
