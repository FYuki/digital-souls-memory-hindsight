"""Atomic profile replacement, serialized with author deletion."""

from datetime import datetime
from typing import cast

import psycopg

from .character_profile.domain import CharacterProfile, ProfileValidationError
from .evidence.ports import EvidenceUnavailable
from .postgres_evidence import PostgresEvidenceStore
from .voice.domain import Span


class PostgresCharacterProfileStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def save(
        self, character_id: str, char_limit: int, entries: tuple[Span, ...], updated_at: datetime
    ) -> CharacterProfile:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                targets = sorted({entry.turn_id for entry in entries})
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
                        raise ProfileValidationError()
                    deleted = connection.execute(
                        "SELECT turn_id FROM turn_tombstones WHERE turn_id = ANY(%s) "
                        "UNION SELECT unnest(target_turn_ids) FROM deletion_requests "
                        "WHERE status <> 'completed' AND target_turn_ids && %s::text[]",
                        (targets, targets),
                    ).fetchall()
                    if deleted:
                        raise ProfileValidationError()
                row = connection.execute(
                    "INSERT INTO character_profiles "
                    "(character_id, char_limit, revision, updated_at) "
                    "VALUES (%s,%s,1,%s) ON CONFLICT (character_id) DO UPDATE SET "
                    "char_limit = EXCLUDED.char_limit, revision = character_profiles.revision + 1, "
                    "updated_at = EXCLUDED.updated_at RETURNING revision",
                    (character_id, char_limit, updated_at),
                ).fetchone()
                if row is None:
                    raise EvidenceUnavailable()
                revision = cast(int, row[0])
                connection.execute(
                    "DELETE FROM character_profile_entries WHERE character_id = %s",
                    (character_id,),
                )
                for position, entry in enumerate(entries):
                    connection.execute(
                        "INSERT INTO character_profile_entries "
                        "(character_id, position, turn_id, span_start, span_end) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (character_id, position, entry.turn_id, entry.start, entry.end),
                    )
                return CharacterProfile(character_id, char_limit, revision, entries, updated_at)
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def get(self, character_id: str) -> CharacterProfile | None:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                # Header and entries must describe the same committed revision.
                connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                row = connection.execute(
                    "SELECT character_id, char_limit, revision, updated_at "
                    "FROM character_profiles WHERE character_id = %s",
                    (character_id,),
                ).fetchone()
                if row is None:
                    return None
                rows = connection.execute(
                    "SELECT turn_id, span_start, span_end FROM character_profile_entries "
                    "WHERE character_id = %s ORDER BY position",
                    (character_id,),
                ).fetchall()
                entries = tuple(Span(str(r[0]), cast(int, r[1]), cast(int, r[2])) for r in rows)
                return CharacterProfile(
                    str(row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    entries,
                    cast(datetime, row[3]),
                )
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
