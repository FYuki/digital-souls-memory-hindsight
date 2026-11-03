"""The immutable selection shared by the response and its provision record."""

from dataclasses import dataclass

from ..identity.ports import IdentitySnapshot
from ..voice.domain import VoiceExemplar


@dataclass(frozen=True)
class ProfileEntry:
    turn_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class ResidentProfile:
    revision: int
    entries: tuple[ProfileEntry, ...]


@dataclass(frozen=True)
class ResidentContext:
    provision_id: str
    identity: IdentitySnapshot
    voice_exemplars: tuple[VoiceExemplar, ...]
    character_profile: ResidentProfile | None
