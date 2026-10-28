"""Explicit retain operations and ports shared by the ledger and worker."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5


def operation_id(turn_id: str, bank_id: str, document_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, json.dumps([turn_id, bank_id, document_id])))


@dataclass(frozen=True)
class RetainTarget:
    bank_id: str
    document_id: str
    content: str


@dataclass(frozen=True)
class RetainOperation:
    turn_id: str
    bank_id: str
    document_id: str
    content: str
    occurred_at: datetime

    @property
    def operation_id(self) -> str:
        return operation_id(self.turn_id, self.bank_id, self.document_id)


@dataclass(frozen=True)
class OutboxItem:
    turn_id: str
    bank_id: str
    document_id: str
    content: str
    occurred_at: datetime
    status: str
    attempts: int
    available_at: datetime
    lease_token: str | None
    lease_until: datetime | None
    last_error: str | None
    completed_at: datetime | None

    @property
    def operation(self) -> RetainOperation:
        return RetainOperation(
            self.turn_id, self.bank_id, self.document_id, self.content, self.occurred_at
        )


@dataclass(frozen=True)
class RetainResult:
    status: Literal["completed", "pending", "failed"]


class RetainExecutor(Protocol):
    async def execute(self, operation: RetainOperation) -> RetainResult: ...


class OutboxStore(Protocol):
    def claim(self, now: datetime, lease_for: timedelta) -> OutboxItem | None: ...
    def complete(self, item: OutboxItem, now: datetime) -> bool: ...
    def defer(
        self, item: OutboxItem, now: datetime, available_at: datetime, reason: str
    ) -> bool: ...
    def fail(self, item: OutboxItem, now: datetime, reason: str) -> bool: ...
