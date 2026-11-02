"""Accept immutable target snapshots and coordinate recorded deletion transitions."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from ..evidence.ports import EvidenceNotFound
from ..evidence.service import EvidenceService
from .domain import DeletionRequest, DeletionStatus, same_selection, transition
from .ports import DeletionConflict, DeletionNotFound, DeletionStore, DeletionTargetNotFound


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeletionService:
    def __init__(
        self,
        store: DeletionStore,
        evidence: EvidenceService,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._evidence = evidence
        self._clock = clock

    def accept(
        self,
        request_id: str,
        requested_by: str,
        target_turn_ids: tuple[str, ...] | None,
        user_id: str | None,
    ) -> DeletionRequest:
        previous = self._store.get(request_id)
        if previous is not None:
            if not same_selection(previous, target_turn_ids, user_id):
                raise DeletionConflict()
            return previous
        if user_id is not None:
            targets = self._evidence.list_user_turns(user_id)
        else:
            assert target_turn_ids is not None
            try:
                targets = tuple(
                    self._evidence.get_turn(identifier)
                    for identifier in sorted(set(target_turn_ids))
                )
            except EvidenceNotFound:
                raise DeletionTargetNotFound() from None
        request = DeletionRequest(
            request_id=request_id,
            requested_by=requested_by,
            target_turn_ids=tuple(sorted({turn.turn_id for turn in targets})),
            status="accepted",
            cascade_result=None,
            error=None,
            requested_at=self._clock(),
            completed_at=None,
            user_id=user_id,
        )
        blocks = frozenset(
            (character, turn.session_id) for turn in targets for character in turn.perceived_by
        )
        saved = self._store.add(request, blocks)
        if not same_selection(saved, target_turn_ids, user_id):
            raise DeletionConflict()
        return saved

    def get(self, request_id: str) -> DeletionRequest:
        request = self._store.get(request_id)
        if request is None:
            raise DeletionNotFound()
        return request

    def transition(
        self,
        request_id: str,
        status: DeletionStatus,
        *,
        cascade_result: Mapping[str, object] | None = None,
        error: str | None = None,
    ) -> DeletionRequest:
        previous = self.get(request_id)
        updated = transition(
            previous, status, self._clock(), cascade_result=cascade_result, error=error
        )
        return self._store.save_transition(previous.status, updated)

    def blocked_sessions(self, character_id: str) -> frozenset[str]:
        return self._store.blocked_sessions(character_id)
