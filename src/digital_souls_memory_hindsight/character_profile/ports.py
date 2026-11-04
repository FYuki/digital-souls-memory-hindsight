"""Persistence boundary for character profile settings."""

from datetime import datetime
from typing import Protocol

from ..voice.domain import Span
from .domain import CharacterProfile


class ProfileNotFound(Exception):
    """No profile has been configured for this character."""


class CharacterProfileStore(Protocol):
    def save(
        self, character_id: str, char_limit: int, entries: tuple[Span, ...], updated_at: datetime
    ) -> CharacterProfile: ...

    def get(self, character_id: str) -> CharacterProfile | None: ...
