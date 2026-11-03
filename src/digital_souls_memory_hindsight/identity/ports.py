"""Identity values and the transactional persistence and generation boundaries."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class IdentityConflict(Exception):
    """An author seed is already registered for this character."""


class IdentityNotFound(Exception):
    """The requested identity version or prerequisite is absent."""


class IdentityUnavailable(Exception):
    """Identity persistence or generation is unavailable."""


@dataclass(frozen=True)
class CoreIdentity:
    character_id: str
    version: int
    values: tuple[str, ...]
    prohibitions: tuple[str, ...]
    registered_at: datetime


@dataclass(frozen=True)
class Temperament:
    character_id: str
    version: int
    description: str
    registered_at: datetime


@dataclass(frozen=True)
class PersonalityProfile:
    character_id: str
    version: int
    aspects: Mapping[str, float]
    description: str
    renderer_version: str
    temperament_version: int
    registered_at: datetime


@dataclass(frozen=True)
class IdentitySnapshot:
    core_identity: CoreIdentity | None
    temperament: Temperament | None
    personality_profile: PersonalityProfile | None


class LLMGateway(Protocol):
    def render_personality(
        self, aspects: dict[str, float], temperament: Temperament, renderer_version: str
    ) -> str: ...


class IdentityTransaction(Protocol):
    def get_core(self, version: int | None) -> CoreIdentity | None: ...
    def get_temperament(self, version: int | None) -> Temperament | None: ...
    def get_profile(self, version: int | None) -> PersonalityProfile | None: ...
    def add_core(self, value: CoreIdentity) -> None: ...
    def add_temperament(self, value: Temperament) -> None: ...
    def add_profile(self, value: PersonalityProfile) -> None: ...


class IdentityStore(Protocol):
    def transaction(self, character_id: str) -> AbstractContextManager[IdentityTransaction]: ...
    def current(self, character_id: str) -> IdentitySnapshot: ...
    def get_core(self, character_id: str, version: int) -> CoreIdentity | None: ...
    def get_temperament(self, character_id: str, version: int) -> Temperament | None: ...
    def get_profile(self, character_id: str, version: int) -> PersonalityProfile | None: ...
