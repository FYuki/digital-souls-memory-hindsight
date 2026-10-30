"""Explicit retain operations and ports shared by the ledger and worker."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5


def operation_id(turn_id: str, bank_id: str, document_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, json.dumps([turn_id, bank_id, document_id])))


@dataclass(frozen=True)
class RetainOperation:
    turn_id: str | None
    bank_id: str
    document_id: str
    content: str
    occurred_at: datetime
    update_mode: Literal["append", "replace", "delete"] = "append"
    source_scope_id: int | None = None
    deletion_request_id: str | None = None

    @property
    def operation_id(self) -> str:
        if self.deletion_request_id is not None:
            if (
                self.update_mode == "append"
                or self.turn_id is not None
                or self.source_scope_id is not None
            ):
                raise ValueError(
                    "Deletion rebuild requires a replace/delete without a turn or scope"
                )
            return str(
                uuid5(
                    NAMESPACE_URL,
                    json.dumps(
                        [
                            "deletion-rebuild",
                            self.update_mode,
                            self.deletion_request_id,
                            self.bank_id,
                            self.document_id,
                        ]
                    ),
                )
            )
        if self.update_mode in {"replace", "delete"}:
            if self.source_scope_id is None:
                raise ValueError(f"{self.update_mode.capitalize()} requires a saved delivery scope")
            return str(
                uuid5(
                    NAMESPACE_URL,
                    json.dumps(
                        [
                            self.update_mode,
                            self.turn_id,
                            self.bank_id,
                            self.document_id,
                            self.source_scope_id,
                        ]
                    ),
                )
            )
        if self.turn_id is None:
            raise ValueError("Append requires a turn")
        return operation_id(self.turn_id, self.bank_id, self.document_id)


@dataclass(frozen=True)
class OutboxItem:
    turn_id: str | None
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
    update_mode: Literal["append", "replace", "delete"] = "append"
    source_scope_id: int | None = None
    deletion_request_id: str | None = None

    @property
    def operation(self) -> RetainOperation:
        return RetainOperation(
            self.turn_id,
            self.bank_id,
            self.document_id,
            self.content,
            self.occurred_at,
            self.update_mode,
            self.source_scope_id,
            self.deletion_request_id,
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
