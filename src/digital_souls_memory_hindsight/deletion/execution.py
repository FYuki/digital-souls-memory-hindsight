"""Resume durable deletion stages, waiting for rebuild and consolidation completion."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from ..evidence.projection import bank_id
from .domain import DeletionItem, DeletionRequest
from .ports import DeletionPersistence, DeletionQueue, DeletionStepResult, MemoryIndex
from .service import DeletionService


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_cascade_result(
    request: DeletionRequest, progress: Mapping[str, object]
) -> dict[str, object]:
    return {
        "deleted_turn_ids": list(request.target_turn_ids),
        "rebuilt_documents": progress["rebuilt_documents"],
        "deleted_documents": progress["deleted_documents"],
        "deleted_anchors": progress["deleted_anchors"],
        "deleted_provision_ids": progress["deleted_provision_ids"],
    }


class DeletionExecutor:
    def __init__(
        self,
        service: DeletionService,
        store: DeletionPersistence,
        memory: MemoryIndex | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._service = service
        self._store = store
        self._memory = memory
        self._clock = clock

    @property
    def queue(self) -> DeletionQueue:
        return self._store

    async def step(self, item: DeletionItem) -> DeletionStepResult:
        request = await asyncio.to_thread(self._service.get, item.request_id)
        if request.status in {"completed", "failed"}:
            return "completed"
        if self._memory is None:
            return "not_configured"
        if request.status == "accepted":
            request = await asyncio.to_thread(
                self._service.transition, item.request_id, "processing"
            )
        pairs = await asyncio.to_thread(self._store.affected_pairs, item.request_id)
        characters = sorted({character for character, _ in pairs})
        if not item.progress.get("ledger_done"):
            for character in characters:
                if await self._memory.bank_busy(bank_id(character)):
                    return "waiting"
            if "memory_snapshot" not in item.progress:
                snapshot = {
                    character: sorted(await self._memory.memory_ids(bank_id(character)))
                    for character in characters
                }
                progress = {**item.progress, "memory_snapshot": snapshot}
                await asyncio.to_thread(self._store.save_progress, item, progress, self._clock())
                item = replace(item, progress=progress)
            progress = await asyncio.to_thread(
                self._store.delete_turns, item, request, self._clock()
            )
            item = replace(item, progress=progress)
        statuses = await asyncio.to_thread(self._store.rebuild_statuses, item.request_id)
        if "failed" in statuses:
            await asyncio.to_thread(
                self._service.transition, item.request_id, "failed", error="rebuild_failed"
            )
            return "failed"
        expected = len(cast(list[object], item.progress["rebuilt_documents"])) + len(
            cast(list[object], item.progress["deleted_documents"])
        )
        if len(statuses) != expected or any(status != "completed" for status in statuses):
            return "waiting"
        for character in characters:
            if await self._memory.bank_busy(bank_id(character)):
                return "waiting"
        snapshot = cast(dict[str, list[str]], item.progress["memory_snapshot"])
        for character in characters:
            current = await self._memory.memory_ids(bank_id(character))
            removed = frozenset(snapshot[character]) - current
            progress = await asyncio.to_thread(
                self._store.delete_provisions_for_memories, item, character, removed, self._clock()
            )
            item = replace(item, progress=progress)
        await asyncio.to_thread(
            self._service.transition,
            item.request_id,
            "completed",
            cascade_result=build_cascade_result(request, item.progress),
        )
        return "completed"
