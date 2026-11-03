"""Read state only while its generation and visibility remain current."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..evidence.domain import StateKey
from ..evidence.service import EvidenceService
from .domain import DEFAULT_TTL_SECONDS, CharacterState, is_fresh
from .ports import CharacterStateStore, StateDeletion


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CharacterStateService:
    def __init__(
        self,
        store: CharacterStateStore,
        evidence: EvidenceService,
        deletion: StateDeletion,
        *,
        clock: Callable[[], datetime] = _utc_now,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("State TTL must be positive")
        self._store = store
        self._evidence = evidence
        self._deletion = deletion
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)

    def get(self, character_id: str, session_id: str, surface: str) -> CharacterState | None:
        self._evidence.require_character(character_id)
        if self._evidence.get_session(session_id).ended_at is not None:
            return None
        if session_id in self._deletion.blocked_sessions(character_id):
            return None
        value = self._store.get(StateKey(character_id, session_id, surface))
        if value is None or value.state is None or value.generated_at is None:
            return None
        if value.state_generation != value.generation:
            return None
        if not is_fresh(value.generated_at, self._clock(), self._ttl):
            return None
        return value.state
