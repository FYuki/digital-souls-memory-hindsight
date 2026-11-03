"""Atomic generation and lease checks for volatile state persistence."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

import psycopg

from .character_state.domain import CharacterState
from .character_state.ports import StateJob, StoredState
from .evidence.domain import StateKey
from .evidence.ports import EvidenceUnavailable


class PostgresCharacterStateStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                yield connection
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def get(self, key: StateKey) -> StoredState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT generation, state_generation, mood, relationship_temperature, "
                "current_topic, generated_at FROM character_states "
                "WHERE character_id=%s AND session_id=%s AND surface=%s",
                key,
            ).fetchone()
        if row is None:
            return None
        state = (
            None
            if row[2] is None
            else CharacterState(cast(str, row[2]), cast(str, row[3]), cast(str, row[4]))
        )
        return StoredState(
            cast(int, row[0]), cast(int | None, row[1]), state, cast(datetime | None, row[5])
        )

    def claim(self, now: datetime, lease_for: timedelta) -> StateJob | None:
        if lease_for <= timedelta(0):
            raise ValueError("Lease must be positive")
        with self._connection() as connection:
            row = connection.execute(
                "WITH candidate AS (SELECT character_id, session_id, surface "
                "FROM character_state_outbox WHERE "
                "(status='pending' AND available_at <= %s) OR "
                "(status='processing' AND lease_until <= %s) "
                "ORDER BY available_at, character_id, session_id, surface "
                "FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE character_state_outbox o SET status='processing', attempts=o.attempts+1, "
                "lease_token=%s, lease_until=%s FROM candidate c "
                "WHERE (o.character_id,o.session_id,o.surface)="
                "(c.character_id,c.session_id,c.surface) "
                "RETURNING o.character_id,o.session_id,o.surface,o.generation,o.attempts,"
                "o.lease_token,o.lease_until",
                (now, now, str(uuid4()), now + lease_for),
            ).fetchone()
        if row is None:
            return None
        return StateJob(
            StateKey(cast(str, row[0]), cast(str, row[1]), cast(str, row[2])),
            cast(int, row[3]),
            cast(int, row[4]),
            cast(str, row[5]),
            cast(datetime, row[6]),
        )

    def save(self, job: StateJob, state: CharacterState, now: datetime) -> bool:
        with self._connection() as connection:
            # Input mutations lock the state before its queue row; keep that order here.
            current = connection.execute(
                "SELECT generation FROM character_states "
                "WHERE character_id=%s AND session_id=%s AND surface=%s "
                "AND generation=%s FOR UPDATE",
                (*job.key, job.generation),
            ).fetchone()
            if current is None:
                return False
            owned = connection.execute(
                "SELECT generation FROM character_state_outbox "
                "WHERE character_id=%s AND session_id=%s AND surface=%s AND generation=%s "
                "AND status='processing' AND lease_token=%s AND lease_until > %s FOR UPDATE",
                (*job.key, job.generation, job.lease_token, now),
            ).fetchone()
            if owned is None:
                return False
            changed = connection.execute(
                "UPDATE character_states SET mood=%s, relationship_temperature=%s, "
                "current_topic=%s, state_generation=%s, generated_at=%s "
                "WHERE character_id=%s AND session_id=%s AND surface=%s AND generation=%s "
                "RETURNING generation",
                (
                    state.mood,
                    state.relationship_temperature,
                    state.current_topic,
                    job.generation,
                    now,
                    *job.key,
                    job.generation,
                ),
            ).fetchone()
            if changed is None:
                return False
            return self._finish(connection, job, now, "completed", now, None)

    def complete(self, job: StateJob, now: datetime) -> bool:
        with self._connection() as connection:
            return self._finish(connection, job, now, "completed", now, None)

    def defer(self, job: StateJob, now: datetime, available_at: datetime, reason: str) -> bool:
        with self._connection() as connection:
            return self._finish(connection, job, now, "pending", available_at, reason)

    def fail(self, job: StateJob, now: datetime, reason: str) -> bool:
        with self._connection() as connection:
            return self._finish(connection, job, now, "failed", now, reason)

    def _finish(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        job: StateJob,
        now: datetime,
        status: str,
        available_at: datetime,
        reason: str | None,
    ) -> bool:
        row = connection.execute(
            "UPDATE character_state_outbox SET status=%s, available_at=%s, last_error=%s, "
            "completed_at=%s, lease_token=NULL, lease_until=NULL "
            "WHERE character_id=%s AND session_id=%s AND surface=%s AND generation=%s "
            "AND status='processing' AND lease_token=%s AND lease_until > %s RETURNING generation",
            (
                status,
                available_at,
                reason,
                now if status == "completed" else None,
                *job.key,
                job.generation,
                job.lease_token,
                now,
            ),
        ).fetchone()
        return row is not None
