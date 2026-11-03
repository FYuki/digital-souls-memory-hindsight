"""Select a consistent context, then commit it after fencing source deletions."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from ..character_profile.service import available_entries
from ..evidence.ports import EvidenceUnavailable
from ..evidence.service import EvidenceService
from ..recall.ports import ProvisionSpan
from ..voice.domain import rank
from ..voice.service import exemplar_available
from .domain import ProfileEntry, ResidentContext, ResidentProfile
from .ports import ResidentProvisionRecord, ResidentRead, ResidentSourcesChanged, ResidentStore

MAX_PROVISION_ATTEMPTS = 3


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _select(read: ResidentRead, character_id: str, query: str | None) -> ResidentContext:
    evidence = EvidenceService(read.evidence, None)
    identity = read.identity()
    voices = rank(
        tuple(v for v in read.exemplars() if exemplar_available(v, evidence, read)), query
    )
    profile = read.profile()
    selected_profile = None
    if profile is not None:
        entries = available_entries(character_id, profile.entries, evidence, read)
        selected_profile = ResidentProfile(
            profile.revision,
            tuple(
                ProfileEntry(
                    s.turn_id, s.start, s.end, evidence.get_turn(s.turn_id).text[s.start : s.end]
                )
                for s in entries
            ),
        )
    return ResidentContext(str(uuid4()), identity, voices, selected_profile)


class ResidentService:
    def __init__(
        self,
        store: ResidentStore,
        evidence: EvidenceService,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        self._store = store
        self._evidence = evidence
        self._clock = clock
        self._timer = timer

    def provide(
        self,
        character_id: str,
        session_id: str,
        surface: str,
        query: str | None,
        sensitivity: str | None,
        classifier_version: str | None,
    ) -> ResidentContext:
        started = self._timer()
        self._evidence.require_character(character_id)
        self._evidence.get_session(session_id)
        for _ in range(MAX_PROVISION_ATTEMPTS):
            with self._store.snapshot(character_id) as read:
                context = _select(read, character_id, query)
            core = context.identity.core_identity
            temperament = context.identity.temperament
            personality = context.identity.personality_profile
            profile = context.character_profile
            spans = (
                ()
                if profile is None
                else tuple(ProvisionSpan(s.turn_id, s.start, s.end) for s in profile.entries)
            )
            sources = frozenset(
                s.turn_id
                for v in context.voice_exemplars
                for s in (v.answer_span, v.question_span)
                if s is not None
            ) | frozenset(s.turn_id for s in spans)
            record = ResidentProvisionRecord(
                context.provision_id,
                character_id,
                session_id,
                surface,
                query,
                sensitivity if query is not None else None,
                classifier_version if query is not None else None,
                None if core is None else core.version,
                None if temperament is None else temperament.version,
                None if personality is None else personality.version,
                tuple(v.exemplar_id for v in context.voice_exemplars),
                None if profile is None else profile.revision,
                spans,
                int((self._timer() - started) * 1000),
                self._clock(),
            )
            try:
                self._store.add(record, sources)
            except ResidentSourcesChanged:
                continue
            return context
        raise EvidenceUnavailable()
