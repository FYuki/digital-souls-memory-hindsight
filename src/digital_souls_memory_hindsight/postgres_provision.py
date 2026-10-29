"""Append-only provision storage, committed before recall returns."""

import psycopg
from psycopg.types.json import Jsonb

from .evidence.ports import EvidenceUnavailable
from .recall.ports import ProvisionConflict, ProvisionRecord


class PostgresProvisionStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def add(self, value: ProvisionRecord) -> None:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                inserted = connection.execute(
                    "INSERT INTO provision_records "
                    "(provision_id, kind, character_id, session_id, surface, query, turn_id, "
                    "sensitivity, classifier_version, memory_ids, source_spans, "
                    "verbatim_anchor_ids, temperament_version, personality_version, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (provision_id) DO NOTHING RETURNING provision_id",
                    (
                        value.provision_id,
                        value.kind,
                        value.character_id,
                        value.session_id,
                        value.surface,
                        value.query,
                        value.turn_id,
                        value.sensitivity,
                        value.classifier_version,
                        list(value.memory_ids),
                        Jsonb(
                            [
                                {"turn_id": span.turn_id, "start": span.start, "end": span.end}
                                for span in value.source_spans
                            ]
                        ),
                        list(value.verbatim_anchor_ids),
                        value.temperament_version,
                        value.personality_version,
                        value.created_at,
                    ),
                ).fetchone()
                if inserted is None:
                    raise ProvisionConflict()
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
