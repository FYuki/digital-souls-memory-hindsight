"""Character projection identities and immutable document snapshots."""

import json
from uuid import NAMESPACE_URL, uuid5

from ..outbox import RetainOperation
from .delivery import DeliveryScope, projection_text
from .domain import Turn
from .ports import EvidenceStore


def bank_id(character_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, json.dumps(["character-bank", character_id])))


def document_id(session_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, json.dumps(["session-document", session_id])))


def enqueue_projection(store: EvidenceStore, turn: Turn, scope: DeliveryScope | None) -> None:
    for character in turn.perceived_by:
        content = projection_text(turn, character, scope)
        if content:
            store.add_retain(
                RetainOperation(
                    turn.turn_id,
                    bank_id(character),
                    document_id(turn.session_id),
                    content,
                    turn.occurred_at,
                )
            )


def update_projection(store: EvidenceStore, turn: Turn, scope: DeliveryScope) -> None:
    for character in turn.perceived_by:
        if character == turn.actor:
            continue
        bank = bank_id(character)
        previous = store.list_projection_turns(turn.session_id, bank)
        content = projection_text(turn, character, scope)
        if not any(value.turn_id == turn.turn_id for value in previous):
            if content:
                store.add_retain(
                    RetainOperation(
                        turn.turn_id, bank, document_id(turn.session_id), content, turn.occurred_at
                    )
                )
            continue
        parts = []
        for value in previous:
            latest = None if value.kind == "thought" else store.get_delivery_scope(value.turn_id)
            text = projection_text(value, character, latest)
            if text:
                parts.append(text)
        store.add_retain(
            RetainOperation(
                turn.turn_id,
                bank,
                document_id(turn.session_id),
                "\n\n".join(parts),
                turn.occurred_at,
                "replace",
                scope.scope_id,
            )
        )
