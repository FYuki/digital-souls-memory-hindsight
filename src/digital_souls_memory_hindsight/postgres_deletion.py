"""Transactional persistence of deletion requests and character/session blocks."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from .deletion.domain import (
    ALLOWED,
    DeletionItem,
    DeletionLeaseLost,
    DeletionRequest,
    DeletionStatus,
)
from .deletion.ports import DeletionTransitionRejected
from .evidence.ports import EvidenceUnavailable
from .evidence.projection import bank_id, document_content, document_id
from .outbox import RetainOperation
from .postgres_evidence import PostgresEvidenceStore


class PostgresDeletionStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                yield connection
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def _get(
        self, connection: psycopg.Connection[tuple[object, ...]], request_id: str
    ) -> DeletionRequest | None:
        with connection.cursor(row_factory=class_row(DeletionRequest)) as cursor:
            cursor.execute("SELECT * FROM deletion_requests WHERE request_id = %s", (request_id,))
            return cursor.fetchone()

    def get(self, request_id: str) -> DeletionRequest | None:
        with self._connection() as connection:
            return self._get(connection, request_id)

    def add(self, request: DeletionRequest, blocks: frozenset[tuple[str, str]]) -> DeletionRequest:
        with self._connection() as connection:
            inserted = connection.execute(
                "INSERT INTO deletion_requests "
                "(request_id, requested_by, target_turn_ids, status, cascade_result, error, "
                "requested_at, completed_at, user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
                (
                    request.request_id,
                    request.requested_by,
                    list(request.target_turn_ids),
                    request.status,
                    Jsonb(dict(request.cascade_result))
                    if request.cascade_result is not None
                    else None,
                    request.error,
                    request.requested_at,
                    request.completed_at,
                    request.user_id,
                ),
            ).fetchone()
            if inserted is None:
                previous = self._get(connection, request.request_id)
                if previous is None:
                    raise EvidenceUnavailable()
                return previous
            for character, session in sorted(blocks):
                connection.execute(
                    "INSERT INTO deletion_blocks (request_id, character_id, session_id) "
                    "VALUES (%s,%s,%s)",
                    (request.request_id, character, session),
                )
            connection.execute(
                "INSERT INTO deletion_outbox (request_id, available_at) VALUES (%s, %s)",
                (request.request_id, request.requested_at),
            )
        return request

    def save_transition(
        self, previous_status: DeletionStatus, updated: DeletionRequest
    ) -> DeletionRequest:
        if (previous_status, updated.status) not in ALLOWED:
            raise DeletionTransitionRejected()
        with self._connection() as connection:
            row = connection.execute(
                "UPDATE deletion_requests SET status = %s, cascade_result = %s, error = %s, "
                "completed_at = %s WHERE request_id = %s AND status = %s RETURNING request_id",
                (
                    updated.status,
                    Jsonb(dict(updated.cascade_result))
                    if updated.cascade_result is not None
                    else None,
                    updated.error,
                    updated.completed_at,
                    updated.request_id,
                    previous_status,
                ),
            ).fetchone()
            if row is None:
                raise DeletionTransitionRejected()
        return updated

    def blocked_sessions(self, character_id: str) -> frozenset[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT b.session_id FROM deletion_blocks b "
                "JOIN deletion_requests d USING (request_id) "
                "WHERE d.status <> 'completed' AND b.character_id = %s",
                (character_id,),
            ).fetchall()
            return frozenset(str(row[0]) for row in rows)

    def claim(self, now: datetime, lease_for: timedelta) -> DeletionItem | None:
        if lease_for <= timedelta(0):
            raise ValueError("Lease must be positive")
        with self._connection() as connection:
            with connection.cursor(row_factory=class_row(DeletionItem)) as cursor:
                cursor.execute(
                    "WITH candidate AS (SELECT request_id FROM deletion_outbox WHERE "
                    "(status = 'pending' AND available_at <= %s) OR "
                    "(status = 'processing' AND lease_until <= %s) "
                    "ORDER BY available_at, request_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                    "UPDATE deletion_outbox SET status = 'processing', attempts = attempts + 1, "
                    "lease_token = %s, lease_until = %s WHERE request_id = "
                    "(SELECT request_id FROM candidate) RETURNING deletion_outbox.*",
                    (now, now, str(uuid4()), now + lease_for),
                )
                return cursor.fetchone()

    def complete(self, item: DeletionItem, now: datetime) -> bool:
        return self._finish(item, now, "completed", now, None)

    def defer(self, item: DeletionItem, now: datetime, available_at: datetime, reason: str) -> bool:
        return self._finish(item, now, "pending", available_at, reason)

    def fail(self, item: DeletionItem, now: datetime, reason: str) -> bool:
        return self._finish(item, now, "failed", now, reason)

    def renew(self, item: DeletionItem, now: datetime, lease_for: timedelta) -> bool:
        """Extend only the caller's own live lease; an expired lease is never revived."""
        if lease_for <= timedelta(0):
            raise ValueError("Lease must be positive")
        with self._connection() as connection:
            row = connection.execute(
                "UPDATE deletion_outbox SET lease_until = %s "
                "WHERE request_id = %s AND status = 'processing' "
                "AND lease_token = %s AND lease_until > %s RETURNING request_id",
                (now + lease_for, item.request_id, item.lease_token, now),
            ).fetchone()
            return row is not None

    def _finish(
        self,
        item: DeletionItem,
        now: datetime,
        status: str,
        available_at: datetime,
        reason: str | None,
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "UPDATE deletion_outbox SET status = %s, available_at = %s, last_error = %s, "
                "completed_at = %s, lease_token = NULL, lease_until = NULL "
                "WHERE request_id = %s AND status = 'processing' "
                "AND lease_token = %s AND lease_until > %s RETURNING request_id",
                (
                    status,
                    available_at,
                    reason,
                    now if status == "completed" else None,
                    item.request_id,
                    item.lease_token,
                    now,
                ),
            ).fetchone()
            return row is not None

    @contextmanager
    def _owned_connection(
        self, item: DeletionItem, now: datetime
    ) -> Iterator[tuple[psycopg.Connection[tuple[object, ...]], dict[str, object]]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT progress FROM deletion_outbox WHERE request_id = %s "
                "AND status = 'processing' AND lease_token = %s AND lease_until > %s FOR UPDATE",
                (item.request_id, item.lease_token, now),
            ).fetchone()
            if row is None:
                raise DeletionLeaseLost()
            yield connection, cast(dict[str, object], row[0])

    def save_progress(self, item: DeletionItem, progress: dict[str, object], now: datetime) -> None:
        with self._owned_connection(item, now) as (connection, _):
            connection.execute(
                "UPDATE deletion_outbox SET progress = %s WHERE request_id = %s",
                (Jsonb(progress), item.request_id),
            )

    def affected_pairs(self, request_id: str) -> tuple[tuple[str, str], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT character_id, session_id FROM deletion_blocks WHERE request_id = %s "
                "ORDER BY character_id, session_id",
                (request_id,),
            ).fetchall()
            return tuple((str(row[0]), str(row[1])) for row in rows)

    def rebuild_statuses(self, request_id: str) -> tuple[str, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status FROM outbox WHERE deletion_request_id = %s",
                (request_id,),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def projections_busy(self, request_id: str, now: datetime) -> bool:
        pairs = self.affected_pairs(request_id)
        with self._connection() as connection:
            return self._projections_busy(connection, pairs, now)

    def _projections_busy(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        pairs: tuple[tuple[str, str], ...],
        now: datetime,
    ) -> bool:
        for character, session in pairs:
            rows = connection.execute(
                "SELECT status, lease_until FROM outbox WHERE bank_id = %s "
                "AND document_id = %s AND deletion_request_id IS NULL "
                "ORDER BY sequence_id FOR UPDATE",
                (bank_id(character), document_id(session)),
            ).fetchall()
            if any(
                status == "processing" and until is not None and cast(datetime, until) > now
                for status, until in rows
            ):
                return True
        return False

    def delete_turns(
        self, item: DeletionItem, request: DeletionRequest, now: datetime
    ) -> dict[str, object]:
        with self._owned_connection(item, now) as (connection, saved):
            if saved.get("ledger_done"):
                return saved
            connection.execute("SET LOCAL dsm.evidence_deletion = 'on'")
            targets = list(request.target_turn_ids)
            pair_rows = connection.execute(
                "SELECT character_id, session_id FROM deletion_blocks WHERE request_id = %s "
                "ORDER BY character_id, session_id",
                (request.request_id,),
            ).fetchall()
            pairs = tuple((str(row[0]), str(row[1])) for row in pair_rows)
            sessions = connection.execute(
                "SELECT session_id FROM turns WHERE turn_id = ANY(%s) "
                "UNION SELECT session_id FROM deletion_blocks WHERE request_id = %s",
                (targets, request.request_id),
            ).fetchall()
            evidence = PostgresEvidenceStore(self._url, connection)
            for session in sorted(str(row[0]) for row in sessions):
                evidence.lock_session(session)
            if self._projections_busy(connection, pairs, now):
                return saved
            connection.execute(
                "SELECT turn_id FROM turns WHERE turn_id = ANY(%s) FOR UPDATE", (targets,)
            ).fetchall()
            anchors = connection.execute(
                "DELETE FROM verbatim_anchors WHERE turn_id = ANY(%s) "
                "RETURNING character_id, anchor_id",
                (targets,),
            ).fetchall()
            provisions = connection.execute(
                "DELETE FROM provision_records WHERE EXISTS (SELECT 1 FROM "
                "jsonb_array_elements(source_spans) span WHERE span->>'turn_id' = ANY(%s)) "
                "RETURNING provision_id",
                (targets,),
            ).fetchall()
            provision_ids = {str(row[0]) for row in provisions}
            for character, anchor in anchors:
                rows = connection.execute(
                    "DELETE FROM provision_records WHERE character_id = %s "
                    "AND %s = ANY(verbatim_anchor_ids) RETURNING provision_id",
                    (character, anchor),
                ).fetchall()
                provision_ids.update(str(row[0]) for row in rows)
            removed_operations = connection.execute(
                "DELETE FROM outbox WHERE deletion_request_id IS NULL AND "
                "(turn_id = ANY(%s) OR source_scope_id IN "
                "(SELECT scope_id FROM delivery_scopes WHERE turn_id = ANY(%s))) "
                "RETURNING operation_id, bank_id, document_id, status",
                (targets, targets),
            ).fetchall()
            for character, session in pairs:
                removed_operations.extend(
                    connection.execute(
                        "DELETE FROM outbox WHERE deletion_request_id IS NULL AND bank_id = %s "
                        "AND document_id = %s AND update_mode <> 'append' "
                        "RETURNING operation_id, bank_id, document_id, status",
                        (bank_id(str(character)), document_id(str(session))),
                    ).fetchall()
                )
            connection.execute("DELETE FROM delivery_receipts WHERE turn_id = ANY(%s)", (targets,))
            connection.execute("DELETE FROM delivery_scopes WHERE turn_id = ANY(%s)", (targets,))
            connection.execute("DELETE FROM turns WHERE turn_id = ANY(%s)", (targets,))
            for turn_identifier in targets:
                connection.execute(
                    "INSERT INTO turn_tombstones (turn_id, request_id, deleted_at) "
                    "VALUES (%s,%s,%s) ON CONFLICT (turn_id) DO NOTHING",
                    (turn_identifier, request.request_id, now),
                )
            rebuilt: list[dict[str, str]] = []
            deleted: list[dict[str, str]] = []
            fenced: list[dict[str, str]] = []
            temporary: dict[tuple[str, str], str] = {}
            rebuilds: list[RetainOperation] = []
            for character, session in pairs:
                content = document_content(evidence, session, character)
                remaining = evidence.list_projection_turns(session, bank_id(character))
                timestamp = (
                    max(turn.occurred_at for turn in remaining) if content else request.requested_at
                )
                rebuilds.append(
                    RetainOperation(
                        None,
                        bank_id(character),
                        document_id(session),
                        content,
                        timestamp,
                        "replace" if content else "delete",
                        deletion_request_id=request.request_id,
                    )
                )
                target = {"bank_id": bank_id(character), "document_id": document_id(session)}
                (rebuilt if content else deleted).append(target)
            for identifier, bank, document, status in removed_operations:
                if status == "completed":
                    continue
                rebuild = next(
                    operation
                    for operation in rebuilds
                    if operation.bank_id == bank and operation.document_id == document
                )
                target_document = rebuild.document_id
                content = rebuild.content
                if not content:
                    # Empty retain is rejected. Deduplication spans documents and survives deletion.
                    target_document = "deletion-fence-" + str(
                        uuid5(NAMESPACE_URL, str([request.request_id, bank, document]))
                    )
                    temporary[(str(bank), str(document))] = target_document
                    content = "削除操作の無効化。"
                evidence.add_retain(
                    RetainOperation(
                        None,
                        str(bank),
                        target_document,
                        content,
                        rebuild.occurred_at,
                        "replace",
                        deletion_request_id=request.request_id,
                        fenced_operation_id=str(identifier),
                    )
                )
                fenced.append(
                    {
                        "operation_id": str(identifier),
                        "bank_id": str(bank),
                        "document_id": target_document,
                    }
                )
            fenced_documents = []
            for (bank, _), document in sorted(temporary.items()):
                evidence.add_retain(
                    RetainOperation(
                        None,
                        bank,
                        document,
                        "",
                        request.requested_at,
                        "delete",
                        deletion_request_id=request.request_id,
                    )
                )
                fenced_documents.append({"bank_id": bank, "document_id": document})
            for operation in rebuilds:
                evidence.add_retain(operation)
            progress = {
                **saved,
                "ledger_done": True,
                "deleted_anchors": [
                    {"character_id": str(character), "anchor_id": str(anchor)}
                    for character, anchor in sorted(anchors)
                ],
                "deleted_provision_ids": sorted(provision_ids),
                "rebuilt_documents": rebuilt,
                "deleted_documents": deleted,
                "fenced_operations": fenced,
                "fenced_documents": fenced_documents,
            }
            connection.execute(
                "UPDATE deletion_outbox SET progress = %s WHERE request_id = %s",
                (Jsonb(progress), item.request_id),
            )
            return progress

    def delete_provisions_for_memories(
        self, item: DeletionItem, character: str, ids: frozenset[str], now: datetime
    ) -> dict[str, object]:
        with self._owned_connection(item, now) as (connection, saved):
            rows = connection.execute(
                "DELETE FROM provision_records WHERE character_id = %s "
                "AND memory_ids && %s::text[] "
                "RETURNING provision_id",
                (character, sorted(ids)),
            ).fetchall()
            previous = cast(list[str], saved["deleted_provision_ids"])
            progress = {
                **saved,
                "deleted_provision_ids": sorted(set(previous) | {str(row[0]) for row in rows}),
            }
            connection.execute(
                "UPDATE deletion_outbox SET progress = %s WHERE request_id = %s",
                (Jsonb(progress), item.request_id),
            )
            return progress
