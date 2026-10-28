"""PostgreSQL adapter; each operation commits before its result reaches HTTP."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from .evidence.delivery import DeliveryScope
from .evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
    SessionEnding,
    Turn,
    matches_saved_turn,
)
from .evidence.ports import EvidenceConflict, EvidenceStore, EvidenceUnavailable


class PostgresEvidenceStore:
    def __init__(
        self, database_url: str, connection: psycopg.Connection[tuple[object, ...]] | None = None
    ) -> None:
        self._url = database_url
        self._bound = connection

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        try:
            if self._bound is not None:
                yield self._bound
            else:
                with psycopg.connect(self._url, connect_timeout=5) as connection:
                    connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                    yield connection
        except psycopg.IntegrityError as error:
            raise EvidenceValidationError("Invalid stored evidence") from error
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    @contextmanager
    def transaction(self) -> Iterator[EvidenceStore]:
        with self._connection() as connection:
            yield PostgresEvidenceStore(self._url, connection)

    def add_session(self, value: Session) -> Session:
        with self._connection() as connection:
            inserted = connection.execute(
                "INSERT INTO sessions "
                "(session_id, started_at, ended_at, surface, reports_delivery) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (session_id) DO NOTHING RETURNING session_id",
                (
                    value.session_id,
                    value.started_at,
                    value.ended_at,
                    value.surface,
                    value.reports_delivery,
                ),
            ).fetchone()
            if inserted is None:
                raise EvidenceConflict()
        return value

    def get_session(self, session_id: str) -> Session | None:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(Session)) as cursor,
        ):
            cursor.execute(
                "SELECT s.session_id, s.started_at, COALESCE(e.ended_at, s.ended_at) AS ended_at, "
                "s.surface, s.reports_delivery FROM sessions s "
                "LEFT JOIN session_endings e ON e.session_id = s.session_id "
                "WHERE s.session_id = %s",
                (session_id,),
            )
            return cursor.fetchone()

    def add_turn(self, value: Turn) -> Turn:
        with self._connection() as connection:
            inserted = connection.execute(
                "INSERT INTO turns (turn_id, session_id, surface, actor, kind, text, perceived_by, "
                "addressees, address_mode, reply_to_turn_id, language, register, sensitivity, "
                "classifier_version, occurred_at, recorded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (turn_id) DO NOTHING RETURNING turn_id",
                (
                    value.turn_id,
                    value.session_id,
                    value.surface,
                    value.actor,
                    value.kind,
                    value.text,
                    list(value.perceived_by),
                    list(value.addressees),
                    value.address_mode,
                    value.reply_to_turn_id,
                    value.language,
                    Jsonb(dict(value.register)),
                    value.sensitivity,
                    value.classifier_version,
                    value.occurred_at,
                    value.recorded_at,
                ),
            ).fetchone()
            if inserted is not None:
                return value
            # DO NOTHING waits for the winner; SELECT gets a fresh READ COMMITTED snapshot.
            previous = PostgresEvidenceStore(self._url, connection).get_turn(value.turn_id)
            if previous is None:
                raise EvidenceUnavailable()
            if not matches_saved_turn(value, previous):
                raise EvidenceConflict()
            return previous

    def get_turn(self, turn_id: str) -> Turn | None:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(Turn)) as cursor,
        ):
            cursor.execute("SELECT * FROM turns WHERE turn_id = %s", (turn_id,))
            return cursor.fetchone()

    def get_turn_for_update(self, turn_id: str) -> Turn | None:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(Turn)) as cursor,
        ):
            cursor.execute("SELECT * FROM turns WHERE turn_id = %s FOR UPDATE", (turn_id,))
            return cursor.fetchone()

    def add_delivery_scope(self, value: DeliveryScope) -> DeliveryScope:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO delivery_scopes (turn_id, status, spoken_until, decided_at) "
                "VALUES (%s, %s, %s, %s)",
                (value.turn_id, value.status, value.spoken_until, value.decided_at),
            )
        return value

    def get_delivery_scope(self, turn_id: str) -> DeliveryScope | None:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(DeliveryScope)) as cursor,
        ):
            cursor.execute(
                "SELECT turn_id, status, spoken_until, decided_at FROM delivery_scopes "
                "WHERE turn_id = %s ORDER BY scope_id DESC LIMIT 1",
                (turn_id,),
            )
            return cursor.fetchone()

    def list_pending_deliveries(self, cutoff: datetime) -> tuple[Turn, ...]:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(Turn)) as cursor,
        ):
            cursor.execute(
                "SELECT t.* FROM turns t JOIN LATERAL ("
                "SELECT status FROM delivery_scopes WHERE turn_id = t.turn_id "
                "ORDER BY scope_id DESC LIMIT 1) s ON s.status = 'pending' "
                "WHERE t.recorded_at <= %s ORDER BY t.recorded_at, t.turn_id",
                (cutoff,),
            )
            return tuple(cursor.fetchall())

    def add_receipt(self, value: DeliveryReceipt) -> DeliveryReceipt:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO delivery_receipts "
                "(turn_id, status, spoken_until, reported_by, reported_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    value.turn_id,
                    value.status,
                    value.spoken_until,
                    value.reported_by,
                    value.reported_at,
                ),
            )
        return value

    def list_receipts(self, turn_id: str) -> tuple[DeliveryReceipt, ...]:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(DeliveryReceipt)) as cursor,
        ):
            cursor.execute(
                "SELECT turn_id, status, spoken_until, reported_by, reported_at "
                "FROM delivery_receipts WHERE turn_id = %s ORDER BY receipt_id",
                (turn_id,),
            )
            return tuple(cursor.fetchall())

    def add_ending(self, value: SessionEnding) -> tuple[SessionEnding, bool]:
        with self._connection() as connection:
            inserted = connection.execute(
                "INSERT INTO session_endings (session_id, ended_at, reported_by, reported_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING RETURNING session_id",
                (value.session_id, value.ended_at, value.reported_by, value.reported_at),
            ).fetchone()
            if inserted is not None:
                return value, True
            (previous,) = PostgresEvidenceStore(self._url, connection).list_endings(
                value.session_id
            )
            if (previous.ended_at, previous.reported_by) != (value.ended_at, value.reported_by):
                raise EvidenceConflict()
            return previous, False

    def list_endings(self, session_id: str) -> tuple[SessionEnding, ...]:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(SessionEnding)) as cursor,
        ):
            cursor.execute("SELECT * FROM session_endings WHERE session_id = %s", (session_id,))
            return tuple(cursor.fetchall())

    def list_perceived_turns(self, character_id: str) -> tuple[Turn, ...]:
        with (
            self._connection() as connection,
            connection.cursor(row_factory=class_row(Turn)) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM turns WHERE perceived_by @> ARRAY[%s]::text[] "
                "ORDER BY occurred_at, turn_id",
                (character_id,),
            )
            return tuple(cursor.fetchall())
