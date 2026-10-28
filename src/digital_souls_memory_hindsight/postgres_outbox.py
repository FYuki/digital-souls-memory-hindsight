"""Short PostgreSQL transactions for durable, fenced outbox claims."""

from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import class_row

from .evidence.ports import EvidenceUnavailable
from .outbox import OutboxItem

_COLUMNS = (
    "turn_id, bank_id, document_id, content, occurred_at, status, attempts, "
    "available_at, lease_token, lease_until, last_error, completed_at"
)


class PostgresOutbox:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def get(self, identifier: str) -> OutboxItem | None:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                with connection.cursor(row_factory=class_row(OutboxItem)) as cursor:
                    cursor.execute(
                        f"SELECT {_COLUMNS} FROM outbox WHERE operation_id = %s", (identifier,)
                    )
                    return cursor.fetchone()
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def claim(self, now: datetime, lease_for: timedelta) -> OutboxItem | None:
        if lease_for <= timedelta(0):
            raise ValueError("Lease must be positive")
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                with connection.cursor(row_factory=class_row(OutboxItem)) as cursor:
                    cursor.execute(
                        "WITH candidate AS (SELECT operation_id FROM outbox WHERE "
                        "(status = 'pending' AND available_at <= %s) OR "
                        "(status = 'processing' AND lease_until <= %s) "
                        "ORDER BY available_at, operation_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                        "UPDATE outbox SET status = 'processing', attempts = attempts + 1, "
                        "lease_token = %s, lease_until = %s WHERE operation_id = "
                        "(SELECT operation_id FROM candidate) "
                        f"RETURNING {_COLUMNS}",
                        (now, now, str(uuid4()), now + lease_for),
                    )
                    item = cursor.fetchone()
            return item
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def complete(self, item: OutboxItem, now: datetime) -> bool:
        return self._finish(item, now, "completed", now, None)

    def defer(self, item: OutboxItem, now: datetime, available_at: datetime, reason: str) -> bool:
        return self._finish(item, now, "pending", available_at, reason)

    def fail(self, item: OutboxItem, now: datetime, reason: str) -> bool:
        return self._finish(item, now, "failed", now, reason)

    def _finish(
        self,
        item: OutboxItem,
        now: datetime,
        status: str,
        available_at: datetime,
        reason: str | None,
    ) -> bool:
        try:
            with psycopg.connect(self._url, connect_timeout=5) as connection:
                changed = connection.execute(
                    "UPDATE outbox SET status = %s, available_at = %s, last_error = %s, "
                    "completed_at = %s, lease_token = NULL, lease_until = NULL "
                    "WHERE operation_id = %s AND status = 'processing' "
                    "AND lease_token = %s AND lease_until > %s RETURNING operation_id",
                    (
                        status,
                        available_at,
                        reason,
                        now if status == "completed" else None,
                        item.operation.operation_id,
                        item.lease_token,
                        now,
                    ),
                ).fetchone()
            return changed is not None
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
