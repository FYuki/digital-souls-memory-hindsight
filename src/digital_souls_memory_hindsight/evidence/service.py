"""Evidence registration and queries through the persistence port."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..outbox import RetainOperation, RetainTarget
from .delivery import (
    DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    DeliveryScope,
    initial_delivery_scope,
    projection_text,
)
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EvidenceService:
    def __init__(
        self,
        store: EvidenceStore,
        character_ids: frozenset[str] | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        delivery_timeout_seconds: int = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._character_ids = character_ids
        self._clock = clock
        if delivery_timeout_seconds <= 0:
            raise ValueError("Delivery timeout must be positive")
        self._delivery_timeout = timedelta(seconds=delivery_timeout_seconds)

    def add_session(self, value: Session) -> Session:
        return self._store.add_session(value)

    def get_session(self, session_id: str) -> Session:
        value = self._store.get_session(session_id)
        if value is None:
            raise EvidenceNotFound()
        return value

    def add_turn(self, value: TurnInput, *, retain_targets: tuple[RetainTarget, ...] = ()) -> Turn:
        if self._character_ids is None:
            raise EvidenceUnavailable()
        with self._store.transaction() as store:
            previous = store.get_turn(value.turn_id)
            if previous is not None:
                if not matches_saved_turn(value, previous):
                    raise EvidenceConflict()
                self._enqueue(store, previous, retain_targets)
                return previous
            turn = prepare_turn(value, self._character_ids, self._clock())
            session = store.get_session(turn.session_id)
            if session is None:
                raise EvidenceNotFound()
            if turn.reply_to_turn_id is not None and store.get_turn(turn.reply_to_turn_id) is None:
                raise EvidenceValidationError("Reply target does not exist")
            saved = store.add_turn(turn)
            store.get_turn_for_update(saved.turn_id)
            initial = initial_delivery_scope(saved, session)
            if initial is not None and store.get_delivery_scope(saved.turn_id) is None:
                store.add_delivery_scope(initial)
            self._enqueue(store, saved, retain_targets)
        return saved

    @staticmethod
    def _enqueue(store: EvidenceStore, turn: Turn, targets: tuple[RetainTarget, ...]) -> None:
        for target in targets:
            store.add_retain(
                RetainOperation(
                    turn.turn_id,
                    target.bank_id,
                    target.document_id,
                    target.content,
                    turn.occurred_at,
                )
            )

    def get_turn(self, turn_id: str) -> Turn:
        value = self._store.get_turn(turn_id)
        if value is None:
            raise EvidenceNotFound()
        return value

    def add_receipt(self, value: DeliveryReceipt) -> DeliveryReceipt:
        with self._store.transaction() as store:
            turn = store.get_turn_for_update(value.turn_id)
            if turn is None:
                raise EvidenceNotFound()
            session = store.get_session(turn.session_id)
            if session is None:
                raise EvidenceNotFound()
            validate_receipt(value, turn, session)
            scope = store.get_delivery_scope(turn.turn_id)
            if scope is None:
                raise EvidenceUnavailable("Delivery scope is missing")
            saved = store.add_receipt(value)
            if scope.status != "immediate":
                store.add_delivery_scope(
                    DeliveryScope(
                        turn.turn_id, "receipt_confirmed", value.spoken_until, self._clock()
                    )
                )
        return saved

    def get_delivery_scope(self, turn_id: str) -> DeliveryScope | None:
        turn = self.get_turn(turn_id)
        if turn.kind == "thought":
            return None
        scope = self._store.get_delivery_scope(turn_id)
        if scope is None:
            raise EvidenceUnavailable("Delivery scope is missing")
        return scope

    def get_projection_text(self, turn_id: str, character_id: str) -> str | None:
        turn = self.get_turn(turn_id)
        needs_scope = character_id in turn.perceived_by and character_id != turn.actor
        scope = self.get_delivery_scope(turn_id) if needs_scope else None
        return projection_text(turn, character_id, scope)

    def confirm_expired_deliveries(self) -> tuple[str, ...]:
        now = self._clock()
        confirmed: list[str] = []
        with self._store.transaction() as store:
            for candidate in store.list_pending_deliveries(now - self._delivery_timeout):
                turn = store.get_turn_for_update(candidate.turn_id)
                if turn is None:
                    raise EvidenceNotFound()
                scope = store.get_delivery_scope(turn.turn_id)
                if scope is None:
                    raise EvidenceUnavailable("Delivery scope is missing")
                if scope.status == "pending":
                    store.add_delivery_scope(
                        DeliveryScope(turn.turn_id, "assumed_confirmed", len(turn.text), now)
                    )
                    confirmed.append(turn.turn_id)
        return tuple(confirmed)

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
                SessionEnding(session_id, ended_at, reported_by, self._clock())
            )
        return saved

    def list_endings(self, session_id: str) -> tuple[SessionEnding, ...]:
        self.get_session(session_id)
        return self._store.list_endings(session_id)

    def list_perceived_turns(self, character_id: str) -> tuple[Turn, ...]:
        return self._store.list_perceived_turns(character_id)
