"""Register approved anchors and select them using current perceived text."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..evidence.ports import EvidenceNotFound
from ..evidence.service import EvidenceService
from .ports import AnchorNotFound, AnchorStore, VerbatimAnchor

AnchorProjection = tuple[VerbatimAnchor, str]


@dataclass(frozen=True)
class AnchorItem:
    anchor_id: str
    turn_id: str
    text: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AnchorService:
    def __init__(
        self,
        store: AnchorStore,
        evidence: EvidenceService,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._evidence = evidence
        self._clock = clock

    def register(
        self,
        character_id: str,
        anchor_id: str,
        turn_id: str,
        kind: str,
        reason: str,
        approved_by: str,
    ) -> AnchorProjection:
        self._evidence.require_character(character_id)
        text = self._evidence.get_projection_text(turn_id, character_id)
        if not text:
            raise EvidenceNotFound()
        anchor = VerbatimAnchor(
            character_id, anchor_id, turn_id, kind, reason, approved_by, self._clock()
        )
        self._store.add(anchor)
        return anchor, text

    def get(self, character_id: str, anchor_id: str) -> AnchorProjection:
        self._evidence.require_character(character_id)
        anchor = self._store.get(character_id, anchor_id)
        if anchor is None:
            raise AnchorNotFound()
        text = self._evidence.get_projection_text(anchor.turn_id, character_id)
        if not text:
            raise EvidenceNotFound()
        return anchor, text

    def list(self, character_id: str) -> tuple[AnchorProjection, ...]:
        self._evidence.require_character(character_id)
        projected = []
        for anchor in self._store.list(character_id):
            text = self._evidence.get_projection_text(anchor.turn_id, character_id)
            if text:
                projected.append((anchor, text))
        return tuple(projected)

    def for_recall(
        self, character_id: str, search: str, source_turn_ids: frozenset[str]
    ) -> tuple[AnchorItem, ...]:
        projected = self.list(character_id)
        if not projected:
            return ()
        matches = self._store.match(
            tuple((anchor.anchor_id, text) for anchor, text in projected), search
        )
        return tuple(
            AnchorItem(anchor.anchor_id, anchor.turn_id, text)
            for anchor, text in projected
            if anchor.turn_id in source_turn_ids or anchor.anchor_id in matches
        )
