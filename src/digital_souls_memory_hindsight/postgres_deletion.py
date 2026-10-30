"""Transactional persistence of deletion requests and character/session blocks."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from .deletion.domain import ALLOWED, DeletionRequest, DeletionStatus
from .deletion.ports import DeletionTransitionRejected
from .evidence.ports import EvidenceUnavailable


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
