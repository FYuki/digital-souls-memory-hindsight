"""Evidence registration and queries through the persistence port."""

from datetime import UTC, datetime

from .domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
    SessionEnding,
    Turn,
    TurnInput,
    matches_saved_turn,
    prepare_turn,
    validate_receipt,
)
from .ports import EvidenceConflict, EvidenceNotFound, EvidenceStore, EvidenceUnavailable


class EvidenceService:
    def __init__(self, store: EvidenceStore, character_ids: frozenset[str] | None) -> None:
        self._store = store
        self._character_ids = character_ids

    def add_session(self, value: Session) -> Session:
        return self._store.add_session(value)

    def get_session(self, session_id: str) -> Session:
        value = self._store.get_session(session_id)
        if value is None:
            raise EvidenceNotFound()
        return value

    def add_turn(self, value: TurnInput) -> Turn:
        if self._character_ids is None:
            raise EvidenceUnavailable()
        with self._store.transaction() as store:
            previous = store.get_turn(value.turn_id)
            if previous is not None:
                if not matches_saved_turn(value, previous):
                    raise EvidenceConflict()
                return previous
            turn = prepare_turn(value, self._character_ids, datetime.now(UTC))
            if store.get_session(turn.session_id) is None:
                raise EvidenceNotFound()
            if turn.reply_to_turn_id is not None and store.get_turn(turn.reply_to_turn_id) is None:
                raise EvidenceValidationError("Reply target does not exist")
            saved = store.add_turn(turn)
        return saved

    def get_turn(self, turn_id: str) -> Turn:
        value = self._store.get_turn(turn_id)
        if value is None:
            raise EvidenceNotFound()
        return value

    def add_receipt(self, value: DeliveryReceipt) -> DeliveryReceipt:
        with self._store.transaction() as store:
            turn = store.get_turn(value.turn_id)
            if turn is None:
                raise EvidenceNotFound()
            session = store.get_session(turn.session_id)
            if session is None:
                raise EvidenceNotFound()
            validate_receipt(value, turn, session)
            saved = store.add_receipt(value)
        return saved

    def list_receipts(self, turn_id: str) -> tuple[DeliveryReceipt, ...]:
        self.get_turn(turn_id)
        return self._store.list_receipts(turn_id)

    def add_ending(
        self, session_id: str, ended_at: datetime, reported_by: str
    ) -> tuple[SessionEnding, bool]:
        with self._store.transaction() as store:
            session = store.get_session(session_id)
            if session is None:
                raise EvidenceNotFound()
            if ended_at < session.started_at:
                raise EvidenceValidationError("Session ends before it starts")
            saved = store.add_ending(
                SessionEnding(session_id, ended_at, reported_by, datetime.now(UTC))
            )
        return saved

    def list_endings(self, session_id: str) -> tuple[SessionEnding, ...]:
        self.get_session(session_id)
        return self._store.list_endings(session_id)

    def list_perceived_turns(self, character_id: str) -> tuple[Turn, ...]:
        return self._store.list_perceived_turns(character_id)
