"""Snapshot resident reads and deletion-serialized provision commits."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import psycopg
from psycopg.rows import DictRow, dict_row, tuple_row
from psycopg.types.json import Jsonb

from .character_profile.domain import CharacterProfile
from .evidence.ports import EvidenceUnavailable
from .identity.ports import IdentitySnapshot
from .postgres_character_profile import read_profile
from .postgres_evidence import PostgresEvidenceStore
from .postgres_identity import current_identity
from .postgres_voice import list_exemplars
from .recall.ports import ProvisionConflict
from .resident.ports import ResidentProvisionRecord, ResidentRead, ResidentSourcesChanged
from .voice.domain import VoiceExemplar


class _ResidentRead:
    def __init__(
        self, connection: psycopg.Connection[DictRow], database_url: str, character_id: str
    ) -> None:
        self._connection = connection
        self._character_id = character_id
        self.evidence = PostgresEvidenceStore(
            database_url, cast(psycopg.Connection[tuple[object, ...]], connection)
        )

    def identity(self) -> IdentitySnapshot:
        return current_identity(self._connection, self._character_id)

    def exemplars(self) -> tuple[VoiceExemplar, ...]:
        return list_exemplars(self._connection, self._character_id)

    def profile(self) -> CharacterProfile | None:
        return read_profile(self._connection, self._character_id)

    def turns_under_deletion(self, turn_ids: frozenset[str]) -> frozenset[str]:
        with self._connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute(
                "SELECT DISTINCT target FROM deletion_requests, unnest(target_turn_ids) AS target "
                "WHERE status <> 'completed' AND target = ANY(%s)",
                (sorted(turn_ids),),
            )
            return frozenset(str(row[0]) for row in cursor.fetchall())


class PostgresResidentStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    @contextmanager
    def snapshot(self, character_id: str) -> Iterator[ResidentRead]:
        try:
            with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                connection.read_only = True
                yield _ResidentRead(connection, self._url, character_id)
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def add(self, record: ResidentProvisionRecord, source_turn_ids: frozenset[str]) -> None:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                targets = sorted(source_turn_ids)
                if targets:
                    rows = connection.execute(
                        "SELECT turn_id FROM turns WHERE turn_id = ANY(%s) "
                        "ORDER BY turn_id FOR KEY SHARE",
                        (targets,),
                    ).fetchall()
                    if {str(row[0]) for row in rows} != set(targets):
                        raise ResidentSourcesChanged()
                    # A new statement after waiting must see newly committed acceptance.
                    deleted = connection.execute(
                        "SELECT turn_id FROM turn_tombstones WHERE turn_id = ANY(%s) "
                        "UNION SELECT unnest(target_turn_ids) FROM deletion_requests "
                        "WHERE status <> 'completed' AND target_turn_ids && %s::text[]",
                        (targets, targets),
                    ).fetchall()
                    if deleted:
                        raise ResidentSourcesChanged()
                inserted = connection.execute(
                    "INSERT INTO provision_records (provision_id,kind,character_id,session_id,"
                    "surface,query,turn_id,sensitivity,classifier_version,memory_ids,source_spans,"
                    "verbatim_anchor_ids,memory_trust,source_span_trust,verbatim_anchor_trust,"
                    "core_identity_version,temperament_version,personality_version,exemplar_ids,"
                    "profile_revision,profile_entries,processing_time_ms,created_at) "
                    "VALUES (%s,'resident',%s,%s,%s,%s,NULL,%s,%s,'{}','[]','{}','{}','{}','{}',"
                    "%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (provision_id) DO NOTHING RETURNING provision_id",
                    (
                        record.provision_id,
                        record.character_id,
                        record.session_id,
                        record.surface,
                        record.query,
                        record.sensitivity,
                        record.classifier_version,
                        record.core_identity_version,
                        record.temperament_version,
                        record.personality_version,
                        list(record.exemplar_ids),
                        record.profile_revision,
                        Jsonb(
                            [
                                {"turn_id": s.turn_id, "start": s.start, "end": s.end}
                                for s in record.profile_entries
                            ]
                        ),
                        record.processing_time_ms,
                        record.created_at,
                    ),
                ).fetchone()
                if inserted is None:
                    raise ProvisionConflict()
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
