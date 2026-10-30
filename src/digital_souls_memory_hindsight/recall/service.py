"""Match recalled facts to current, perceived ledger text."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from ..anchors.service import AnchorItem, AnchorService
from ..evidence.domain import EvidenceValidationError, Session
from ..evidence.ports import EvidenceNotFound, EvidenceUnavailable
from ..evidence.projection import bank_id, document_id
from ..evidence.service import EvidenceService, ProjectionSource
from .ports import (
    DeletionBlocks,
    MemoryRecall,
    ProvisionRecord,
    ProvisionSpan,
    ProvisionStore,
    RecalledFact,
    RecallSourceNotFound,
    RecallUnavailable,
)
from .trust import EVIDENCE, INFERRED, Trust

MIN_OVERLAP = 8


@dataclass(frozen=True)
class MemoryItem:
    id: str
    kind: str
    content: str
    source_turn_ids: tuple[str, ...]
    trust: Trust


@dataclass(frozen=True)
class SourceSpan:
    turn_id: str
    start: int
    end: int
    text: str
    actor: str
    language: str
    truncated: bool
    original_start: int
    original_end: int
    trust: Trust


@dataclass(frozen=True)
class RecalledAnchor:
    anchor_id: str
    turn_id: str
    text: str
    trust: Trust


@dataclass(frozen=True)
class RecallProvision:
    provision_id: str
    memories: tuple[MemoryItem, ...]
    source_spans: tuple[SourceSpan, ...]
    context: str | None
    verbatim_anchors: tuple[RecalledAnchor, ...]


def match_sources(
    chunk_text: str, candidates: tuple[ProjectionSource, ...]
) -> tuple[ProjectionSource, ...]:
    chunk = "".join(chunk_text.split())
    if not chunk:
        return ()
    matches = []
    for turn, text in candidates:
        projected = "".join(text.split())
        if not projected:
            continue
        # Short boundary coincidences are not evidence that two texts overlap.
        if (
            projected in chunk
            or chunk in projected
            or any(
                chunk.endswith(projected[:length]) or chunk.startswith(projected[-length:])
                for length in range(MIN_OVERLAP, min(len(chunk), len(projected)) + 1)
            )
        ):
            matches.append((turn, text))
    return tuple(matches)


def _assemble(
    facts: tuple[RecalledFact, ...],
    candidates: dict[str, tuple[ProjectionSource, ...]],
) -> RecallProvision:
    memories: dict[str, MemoryItem] = {}
    spans: dict[str, SourceSpan] = {}
    for fact in facts:
        sources = match_sources(fact.chunk_text, candidates.get(fact.document_id, ()))
        if not sources:
            continue
        identifiers = tuple(turn.turn_id for turn, _ in sources)
        previous = memories.get(fact.id)
        if previous is None:
            memories[fact.id] = MemoryItem(fact.id, fact.kind, fact.text, identifiers, INFERRED)
        else:
            memories[fact.id] = replace(
                previous,
                source_turn_ids=tuple(dict.fromkeys((*previous.source_turn_ids, *identifiers))),
            )
        for turn, text in sources:
            if turn.turn_id not in spans:
                spans[turn.turn_id] = SourceSpan(
                    turn.turn_id,
                    0,
                    len(text),
                    text,
                    turn.actor,
                    turn.language,
                    False,
                    0,
                    len(text),
                    EVIDENCE,
                )
    return RecallProvision(
        str(uuid4()),
        tuple(memories.values()),
        tuple(spans.values()),
        None,
        (),
    )


def provision_record(
    provision: RecallProvision,
    *,
    character_id: str,
    session: Session,
    query: str | None,
    turn_id: str | None,
    sensitivity: str | None,
    classifier_version: str | None,
    created_at: datetime,
) -> ProvisionRecord:
    return ProvisionRecord(
        provision_id=provision.provision_id,
        kind="turn",
        character_id=character_id,
        session_id=session.session_id,
        surface=session.surface,
        query=query,
        turn_id=turn_id,
        sensitivity=sensitivity if query is not None else None,
        classifier_version=classifier_version if query is not None else None,
        memory_ids=tuple(memory.id for memory in provision.memories),
        source_spans=tuple(
            ProvisionSpan(span.turn_id, span.start, span.end) for span in provision.source_spans
        ),
        verbatim_anchor_ids=tuple(anchor.anchor_id for anchor in provision.verbatim_anchors),
        memory_trust=tuple(memory.trust for memory in provision.memories),
        source_span_trust=tuple(span.trust for span in provision.source_spans),
        verbatim_anchor_trust=tuple(anchor.trust for anchor in provision.verbatim_anchors),
        temperament_version=None,
        personality_version=None,
        created_at=created_at,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RecallService:
    def __init__(
        self,
        evidence: EvidenceService,
        memory: MemoryRecall | None,
        provisions: ProvisionStore,
        anchors: AnchorService,
        deletions: DeletionBlocks,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._evidence = evidence
        self._memory = memory
        self._provisions = provisions
        self._anchors = anchors
        self._deletions = deletions
        self._clock = clock

    def _query(
        self, character_id: str, session_id: str, query: str | None, turn_id: str | None
    ) -> tuple[str, Session]:
        self._evidence.require_character(character_id)
        session = self._evidence.get_session(session_id)
        if query is not None:
            return query, session
        assert turn_id is not None
        text = self._evidence.get_projection_text(turn_id, character_id)
        if not text:
            raise RecallSourceNotFound()
        return text, session

    async def recall(
        self,
        character_id: str,
        session_id: str,
        format: Literal["materials", "assembled"],
        query: str | None,
        turn_id: str | None,
        sensitivity: str | None,
        classifier_version: str | None,
    ) -> RecallProvision:
        try:
            search, session = await asyncio.to_thread(
                self._query, character_id, session_id, query, turn_id
            )
            if self._memory is None:
                raise RecallUnavailable()
            facts = await self._memory.recall(bank_id(character_id), search)
            blocked = await asyncio.to_thread(self._deletions.blocked_sessions, character_id)
            blocked_documents = frozenset(document_id(session) for session in blocked)
            facts = tuple(fact for fact in facts if fact.document_id not in blocked_documents)
            documents = frozenset(fact.document_id for fact in facts)
            candidates = (
                await asyncio.to_thread(self._evidence.projection_sources, character_id, documents)
                if documents
                else {}
            )
            provision = _assemble(facts, candidates)
            anchors = await asyncio.to_thread(
                self._anchors.for_recall,
                character_id,
                search,
                frozenset(turn for memory in provision.memories for turn in memory.source_turn_ids),
            )
            anchors = await asyncio.to_thread(self._unblocked_anchors, anchors, blocked)
            provision = await asyncio.to_thread(self._with_anchors, provision, anchors, format)
            record = provision_record(
                provision,
                character_id=character_id,
                session=session,
                query=query,
                turn_id=turn_id,
                sensitivity=sensitivity,
                classifier_version=classifier_version,
                created_at=self._clock(),
            )
            await asyncio.to_thread(self._provisions.add, record)
            return provision
        except EvidenceNotFound:
            raise RecallSourceNotFound() from None
        except (EvidenceUnavailable, EvidenceValidationError):
            raise RecallUnavailable() from None

    def _unblocked_anchors(
        self, anchors: tuple[AnchorItem, ...], blocked: frozenset[str]
    ) -> tuple[AnchorItem, ...]:
        return tuple(
            anchor
            for anchor in anchors
            if self._evidence.get_turn(anchor.turn_id).session_id not in blocked
        )

    def _with_anchors(
        self,
        provision: RecallProvision,
        anchors: tuple[AnchorItem, ...],
        format: Literal["materials", "assembled"],
    ) -> RecallProvision:
        spans = {span.turn_id: span for span in provision.source_spans}
        for anchor in anchors:
            if anchor.turn_id not in spans:
                turn = self._evidence.get_turn(anchor.turn_id)
                text = anchor.text
                spans[turn.turn_id] = SourceSpan(
                    turn.turn_id,
                    0,
                    len(text),
                    text,
                    turn.actor,
                    turn.language,
                    False,
                    0,
                    len(text),
                    EVIDENCE,
                )
        return replace(
            provision,
            verbatim_anchors=tuple(
                RecalledAnchor(anchor.anchor_id, anchor.turn_id, anchor.text, EVIDENCE)
                for anchor in anchors
            ),
            source_spans=tuple(spans.values()),
            context="\n".join(span.text for span in spans.values())
            if format == "assembled"
            else None,
        )
