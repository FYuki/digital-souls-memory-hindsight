"""Evidence registration and queries through the persistence port."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

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
from .projection import bank_id, document_id, enqueue_projection, update_projection

ProjectionSource = tuple[Turn, str]


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

    def add_turn(self, value: TurnInput) -> Turn:
        if self._character_ids is None:
            raise EvidenceUnavailable()
        with self._store.transaction() as store:
            if store.is_tombstoned(value.turn_id):
                raise EvidenceConflict()
            previous = store.get_turn(value.turn_id)
            if previous is not None:
                if not matches_saved_turn(value, previous):
                    raise EvidenceConflict()
                return previous
            turn = prepare_turn(value, self._character_ids, self._clock())
            store.lock_session(turn.session_id)
            if store.is_tombstoned(value.turn_id):
                raise EvidenceConflict()
            previous = store.get_turn(value.turn_id)
            if previous is not None:
                if not matches_saved_turn(value, previous):
                    raise EvidenceConflict()
                return previous
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
            enqueue_projection(store, saved, store.get_delivery_scope(saved.turn_id))
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
            store.lock_session(turn.session_id)
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
                latest = store.add_delivery_scope(
                    DeliveryScope(
                        turn.turn_id, "receipt_confirmed", value.spoken_until, self._clock()
                    )
                )
                if scope.spoken_until != latest.spoken_until:
                    update_projection(store, turn, latest)
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

    def require_character(self, character_id: str) -> None:
        if self._character_ids is None:
            raise EvidenceUnavailable()
        if character_id not in self._character_ids:
            raise EvidenceNotFound()

    def projection_sources(
        self, character_id: str, document_ids: frozenset[str]
    ) -> dict[str, tuple[ProjectionSource, ...]]:
        bank = bank_id(character_id)
        sources: dict[str, tuple[ProjectionSource, ...]] = {}
        for session in self._store.list_projection_sessions(bank):
            document = document_id(session)
            if document not in document_ids:
                continue
            candidates: list[ProjectionSource] = []
            for turn in self._store.list_projection_turns(session, bank):
                text = self.get_projection_text(turn.turn_id, character_id)
                if text:
                    candidates.append((turn, text))
            sources[document] = tuple(candidates)
        return sources

    def confirm_expired_deliveries(self) -> tuple[str, ...]:
        now = self._clock()
        confirmed: list[str] = []
        with self._store.transaction() as store:
            candidates = store.list_pending_deliveries(now - self._delivery_timeout)
            for candidate in sorted(
                candidates, key=lambda value: (value.session_id, value.turn_id)
            ):
                store.lock_session(candidate.session_id)
                turn = store.get_turn_for_update(candidate.turn_id)
                if turn is None:
                    raise EvidenceNotFound()
                scope = store.get_delivery_scope(turn.turn_id)
                if scope is None:
                    raise EvidenceUnavailable("Delivery scope is missing")
                if scope.status == "pending":
                    latest = store.add_delivery_scope(
                        DeliveryScope(turn.turn_id, "assumed_confirmed", len(turn.text), now)
                    )
                    update_projection(store, turn, latest)
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

    def list_user_turns(self, user_id: str) -> tuple[Turn, ...]:
        return self._store.list_user_turns(user_id)
