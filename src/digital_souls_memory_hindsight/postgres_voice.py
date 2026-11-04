"""PostgreSQL voice storage, serialized with source deletion by session locks."""

from datetime import datetime
from typing import cast

import psycopg
from psycopg.rows import tuple_row

from .evidence.ports import EvidenceUnavailable
from .postgres_evidence import PostgresEvidenceStore
from .voice.domain import Span, VoiceExemplar, VoiceReview, VoiceValidationError
from .voice.ports import VoiceConflict

_COLUMNS = (
    "character_id, exemplar_id, source, answer, question, "
    "answer_turn_id, answer_start, answer_end, question_turn_id, question_start, question_end, "
    "approved_by, registered_at"
)


def _restore(row: tuple[object, ...]) -> VoiceExemplar:
    def span(offset: int) -> Span | None:
        return (
            Span(str(row[offset]), cast(int, row[offset + 1]), cast(int, row[offset + 2]))
            if row[offset] is not None
            else None
        )

    return VoiceExemplar(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        cast(str | None, row[4]),
        span(5),
        span(8),
        str(row[11]),
        cast(datetime, row[12]),
    )


def list_exemplars(
    connection: psycopg.Connection[object], character_id: str
) -> tuple[VoiceExemplar, ...]:
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            f"SELECT {_COLUMNS} FROM voice_exemplars WHERE character_id = %s ORDER BY exemplar_id",
            (character_id,),
        )
        return tuple(_restore(row) for row in cursor.fetchall())


class PostgresVoiceStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def reviewed(self, character_id: str) -> bool:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                return (
                    connection.execute(
                        "SELECT 1 FROM voice_consistency_reviews WHERE character_id = %s",
                        (character_id,),
                    ).fetchone()
                    is not None
                )
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def add(self, value: VoiceExemplar, review: VoiceReview | None) -> bool:
        committed_review = False
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                spans = (value.answer_span, value.question_span)
                targets = sorted({span.turn_id for span in spans if span is not None})
                if targets:
                    sessions = connection.execute(
                        "SELECT DISTINCT session_id FROM turns WHERE turn_id = ANY(%s)",
                        (targets,),
                    ).fetchall()
                    evidence = PostgresEvidenceStore(self._url, connection)
                    for session in sorted(str(row[0]) for row in sessions):
                        evidence.lock_session(session)
                    rows = connection.execute(
                        "SELECT turn_id FROM turns WHERE turn_id = ANY(%s) "
                        "ORDER BY turn_id FOR KEY SHARE",
                        (targets,),
                    ).fetchall()
                    if {str(row[0]) for row in rows} != set(targets):
                        raise VoiceValidationError()
                    deleted = connection.execute(
                        "SELECT turn_id FROM turn_tombstones WHERE turn_id = ANY(%s) "
                        "UNION SELECT unnest(target_turn_ids) FROM deletion_requests "
                        "WHERE status <> 'completed' AND target_turn_ids && %s::text[]",
                        (targets, targets),
                    ).fetchall()
                    if deleted:
                        raise VoiceValidationError()
                inserted = connection.execute(
                    f"INSERT INTO voice_exemplars ({_COLUMNS}) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (character_id, exemplar_id) DO NOTHING RETURNING exemplar_id",
                    (
                        value.character_id,
                        value.exemplar_id,
                        value.source,
                        value.answer,
                        value.question,
                        value.answer_span.turn_id if value.answer_span is not None else None,
                        value.answer_span.start if value.answer_span is not None else None,
                        value.answer_span.end if value.answer_span is not None else None,
                        value.question_span.turn_id if value.question_span is not None else None,
                        value.question_span.start if value.question_span is not None else None,
                        value.question_span.end if value.question_span is not None else None,
                        value.approved_by,
                        value.registered_at,
                    ),
                ).fetchone()
                if inserted is None:
                    raise VoiceConflict()
                if review is not None:
                    committed_review = (
                        connection.execute(
                            "INSERT INTO voice_consistency_reviews "
                            "(character_id, exemplar_id, consistent, reason, reviewed_at) "
                            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (character_id) DO NOTHING "
                            "RETURNING character_id",
                            (
                                value.character_id,
                                value.exemplar_id,
                                review.result.consistent,
                                review.result.reason,
                                review.reviewed_at,
                            ),
                        ).fetchone()
                        is not None
                    )
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
        return committed_review

    def get(self, character_id: str, exemplar_id: str) -> VoiceExemplar | None:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM voice_exemplars "
                    "WHERE character_id = %s AND exemplar_id = %s",
                    (character_id, exemplar_id),
                ).fetchone()
                return _restore(row) if row is not None else None
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def list(self, character_id: str) -> tuple[VoiceExemplar, ...]:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                return list_exemplars(connection, character_id)
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
