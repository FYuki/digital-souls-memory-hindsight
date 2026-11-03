"""Generation, persistence and deletion visibility boundaries."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ..evidence.domain import StateKey
from .domain import CharacterState, StateInput


class ContextGateway(Protocol):
    async def generate_state(
        self, character_id: str, session_id: str, surface: str, turns: tuple[StateInput, ...]
    ) -> object: ...


@dataclass(frozen=True)
class StoredState:
    generation: int
    state_generation: int | None
    state: CharacterState | None
    generated_at: datetime | None


@dataclass(frozen=True)
class StateJob:
    key: StateKey
    generation: int
    attempts: int
    lease_token: str
    lease_until: datetime


class CharacterStateStore(Protocol):
    def get(self, key: StateKey) -> StoredState | None: ...
    def claim(self, now: datetime, lease_for: timedelta) -> StateJob | None: ...
    def save(self, job: StateJob, state: CharacterState, now: datetime) -> bool: ...
    def complete(self, job: StateJob, now: datetime) -> bool: ...
    def defer(self, job: StateJob, now: datetime, available_at: datetime, reason: str) -> bool: ...
    def fail(self, job: StateJob, now: datetime, reason: str) -> bool: ...


class StateDeletion(Protocol):
    def blocked_sessions(self, character_id: str) -> frozenset[str]: ...
    def turns_under_deletion(self, turn_ids: frozenset[str]) -> frozenset[str]: ...
