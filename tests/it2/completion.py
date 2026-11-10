"""Completion policy for a bank's outbox, retain and automatic consolidation."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

import psycopg
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import NotFoundException

COMPLETION_WAIT_LIMIT_SECONDS = 1800
COMPLETION_POLL_SECONDS = 1.0


class OutboxStatus(TypedDict):
    operation_id: str
    status: str
    update_mode: str


class OperationStatus(TypedDict):
    operation_id: str
    status: str
    type: str


class CompletionSnapshot(TypedDict):
    outbox: list[OutboxStatus]
    operations: list[OperationStatus]
    pending_consolidation: int
    failed_consolidation: int
    pending_operations: NotRequired[int]
    failed_operations: NotRequired[int]


@dataclass(frozen=True)
class CompletionResult:
    status: Literal["complete", "waiting", "failed", "timeout"]
    reason: str


def evaluate_completion(
    *,
    outbox: Sequence[OutboxStatus],
    operations: Sequence[OperationStatus],
    pending_consolidation: int,
    failed_consolidation: int,
    pending_operations: int = 0,
    failed_operations: int = 0,
) -> CompletionResult:
    known = {"pending", "processing", "completed"}
    failures = [
        (row["operation_id"], row["status"])
        for row in (*outbox, *operations)
        if row["status"] not in known
    ]
    if failures:
        identifier, status = min(failures)
        return CompletionResult("failed", f"{identifier}: {status}")
    if (
        failed_consolidation != 0
        or failed_operations != 0
        or pending_consolidation < 0
        or pending_operations < 0
    ):
        return CompletionResult("failed", "operation or consolidation statistics indicate failure")
    if (
        pending_operations != 0
        or not outbox
        or any(row["status"] != "completed" for row in (*outbox, *operations))
    ):
        return CompletionResult("waiting", "outbox or operations are not complete")
    completed_retains = {
        row["operation_id"]
        for row in operations
        if row["type"] == "retain" and row["status"] == "completed"
    }
    if any(
        row["operation_id"] not in completed_retains
        for row in outbox
        if row["update_mode"] != "delete"
    ):
        return CompletionResult("waiting", "target retain operation is not complete")
    if not any(row["type"] == "consolidation" for row in operations):
        return CompletionResult("waiting", "consolidation operation has not completed")
    if pending_consolidation != 0:
        return CompletionResult("waiting", "source memories await consolidation")
    return CompletionResult("complete", "retain, consolidation and outbox completed")


def _completed(rows: Sequence[OutboxStatus] | Sequence[OperationStatus]) -> str:
    done = sum(row["status"] == "completed" for row in rows)
    return f"{done}/{len(rows)}"


def _summarize(snapshot: CompletionSnapshot) -> str:
    """Counts only, so the reason stays safe to publish in reports."""
    operations = snapshot["operations"]
    by_type = {
        kind: _completed([row for row in operations if row["type"] == kind])
        for kind in ("retain", "consolidation")
    }
    return (
        f"outbox completed {_completed(snapshot['outbox'])}; "
        f"retain completed {by_type['retain']}; "
        f"consolidation completed {by_type['consolidation']}; "
        f"pending_consolidation {snapshot['pending_consolidation']}; "
        f"pending_operations {snapshot.get('pending_operations', 0)}"
    )


def wait_for_completion(
    *,
    snapshot: Callable[[], CompletionSnapshot],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> CompletionResult:
    deadline = clock() + COMPLETION_WAIT_LIMIT_SECONDS
    while True:
        current = snapshot()
        result = evaluate_completion(**current)
        if result.status == "failed":
            return result
        remaining = deadline - clock()
        if remaining < 0 or (remaining == 0 and result.status == "waiting"):
            return CompletionResult(
                "timeout",
                f"completion wait limit exceeded: {result.reason} ({_summarize(current)})",
            )
        if result.status != "waiting":
            return result
        sleep(min(COMPLETION_POLL_SECONDS, remaining))


def _read_outbox(database_url: str, bank_id: str) -> list[OutboxStatus]:
    with psycopg.connect(
        database_url, connect_timeout=5, options="-c default_transaction_read_only=on"
    ) as connection:
        rows: list[tuple[str, str, str]] = connection.execute(
            "SELECT operation_id, status, update_mode FROM outbox "
            "WHERE bank_id = %s ORDER BY sequence_id",
            (bank_id,),
        ).fetchall()
    return [
        {"operation_id": identifier, "status": status, "update_mode": mode}
        for identifier, status, mode in rows
    ]


async def read_completion_snapshot(
    client: Hindsight,
    database_url: str,
    bank_id: str,
) -> CompletionSnapshot:
    operations: list[OperationStatus] = []
    offset = 0
    while True:
        try:
            page = await client.operations.list_operations(
                bank_id=bank_id,
                limit=100,
                offset=offset,
                _request_timeout=10.0,
            )
        except NotFoundException:
            # The worker creates the bank on the first projection. No operation means
            # this can only evaluate as waiting (or failed through the outbox).
            return {
                "outbox": await asyncio.to_thread(_read_outbox, database_url, bank_id),
                "operations": [],
                "pending_consolidation": 0,
                "failed_consolidation": 0,
            }
        operations.extend(
            {
                "operation_id": item.id,
                "status": item.status,
                "type": "retain" if item.task_type == "batch_retain" else item.task_type,
            }
            for item in page.operations
        )
        offset += len(page.operations)
        if offset >= page.total:
            break
        if not page.operations:
            raise RuntimeError("Operation enumeration did not advance")
    stats = await client.banks.get_agent_stats(bank_id=bank_id, refresh=True, _request_timeout=10.0)
    if stats.pending_consolidation is None or stats.failed_consolidation is None:
        raise RuntimeError("Consolidation statistics are unavailable")
    return {
        "outbox": await asyncio.to_thread(_read_outbox, database_url, bank_id),
        "operations": operations,
        "pending_consolidation": stats.pending_consolidation,
        "failed_consolidation": stats.failed_consolidation,
        "pending_operations": stats.pending_operations,
        "failed_operations": stats.failed_operations,
    }
