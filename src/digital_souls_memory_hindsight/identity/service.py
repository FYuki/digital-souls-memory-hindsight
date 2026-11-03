"""Explicit registration is the only source of new identity versions."""

from collections.abc import Callable
from datetime import UTC, datetime
from types import MappingProxyType

from .ports import (
    CoreIdentity,
    IdentityConflict,
    IdentityNotFound,
    IdentitySnapshot,
    IdentityStore,
    IdentityUnavailable,
    LLMGateway,
    PersonalityProfile,
    Temperament,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IdentityService:
    def __init__(
        self,
        store: IdentityStore,
        gateway: LLMGateway | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._clock = clock

    def register_core(
        self, character_id: str, values: tuple[str, ...], prohibitions: tuple[str, ...]
    ) -> CoreIdentity:
        with self._store.transaction(character_id) as transaction:
            previous = transaction.get_core(None)
            value = CoreIdentity(
                character_id,
                1 if previous is None else previous.version + 1,
                values,
                prohibitions,
                self._clock(),
            )
            transaction.add_core(value)
        return value

    def register_temperament(self, character_id: str, description: str) -> Temperament:
        with self._store.transaction(character_id) as transaction:
            previous = transaction.get_temperament(None)
            value = Temperament(
                character_id,
                1 if previous is None else previous.version + 1,
                description,
                self._clock(),
            )
            transaction.add_temperament(value)
        return value

    def author_seed(
        self, character_id: str, aspects: dict[str, float], renderer_version: str
    ) -> PersonalityProfile:
        if self._gateway is None:
            raise IdentityUnavailable()
        with self._store.transaction(character_id) as transaction:
            if transaction.get_profile(None) is not None:
                raise IdentityConflict()
            temperament = transaction.get_temperament(None)
            if temperament is None:
                raise IdentityNotFound()
            try:
                description = self._gateway.render_personality(
                    dict(aspects), temperament, renderer_version
                )
            except Exception as error:
                raise IdentityUnavailable() from error
            if not description.strip():
                raise IdentityUnavailable()
            profile = PersonalityProfile(
                character_id,
                1,
                MappingProxyType(dict(aspects)),
                description,
                renderer_version,
                temperament.version,
                self._clock(),
            )
            transaction.add_profile(profile)
        return profile

    def current(self, character_id: str) -> IdentitySnapshot:
        return self._store.current(character_id)

    def get_core(self, character_id: str, version: int) -> CoreIdentity:
        value = self._store.get_core(character_id, version)
        if value is None:
            raise IdentityNotFound()
        return value

    def get_temperament(self, character_id: str, version: int) -> Temperament:
        value = self._store.get_temperament(character_id, version)
        if value is None:
            raise IdentityNotFound()
        return value

    def get_profile(self, character_id: str, version: int) -> PersonalityProfile:
        value = self._store.get_profile(character_id, version)
        if value is None:
            raise IdentityNotFound()
        return value
