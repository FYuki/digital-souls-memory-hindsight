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
        "deleted_voice_exemplars": progress.get("deleted_voice_exemplars", []),
        "deleted_profile_entries": progress.get("deleted_profile_entries", []),
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
            if await asyncio.to_thread(
                self._store.projections_busy, item.request_id, self._clock()
            ):
                return "waiting"
            for character in characters:
                if await self._memory.bank_busy(bank_id(character)):
                    return "waiting"
            if "memory_snapshot" not in item.progress:
                snapshot = {
                    character: sorted(await self._memory.memory_ids(bank_id(character)))
                    for character in characters
                }
                sources = {
                    character: {
                        identifier: sorted(ids)
                        for identifier, ids in (
                            await self._memory.observation_sources(bank_id(character))
                        ).items()
                    }
                    for character in characters
                }
                progress = {
                    **item.progress,
                    "memory_snapshot": snapshot,
                    "observation_sources": sources,
                }
                await asyncio.to_thread(self._store.save_progress, item, progress, self._clock())
                item = replace(item, progress=progress)
            progress = await asyncio.to_thread(
                self._store.delete_turns, item, request, self._clock()
            )
            item = replace(item, progress=progress)
            if not progress.get("ledger_done"):
                return "waiting"
        statuses = await asyncio.to_thread(self._store.rebuild_statuses, item.request_id)
        if "failed" in statuses:
            await asyncio.to_thread(
                self._service.transition, item.request_id, "failed", error="rebuild_failed"
            )
            return "failed"
        expected = len(cast(list[object], item.progress["rebuilt_documents"])) + len(
            cast(list[object], item.progress["deleted_documents"])
        )
        expected += len(cast(list[object], item.progress["fenced_operations"])) + len(
            cast(list[object], item.progress["fenced_documents"])
        )
        if len(statuses) != expected or any(status != "completed" for status in statuses):
            return "waiting"
        for character in characters:
            if await self._memory.bank_busy(bank_id(character)):
                return "waiting"
        snapshot = cast(dict[str, list[str]], item.progress["memory_snapshot"])
        current_ids = {
            character: await self._memory.memory_ids(bank_id(character)) for character in characters
        }
        residue = await self._repair_residue(item, characters, current_ids)
        if residue is not None:
            return residue
        for character in characters:
            removed = frozenset(snapshot[character]) - current_ids[character]
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

    async def _repair_residue(
        self,
        item: DeletionItem,
        characters: list[str],
        current_ids: dict[str, frozenset[str]],
    ) -> DeletionStepResult | None:
        assert self._memory is not None
        snapshot = cast(dict[str, list[str]], item.progress["memory_snapshot"])
        before = cast(dict[str, dict[str, list[str]]], item.progress["observation_sources"])
        repaired = cast(dict[str, list[str]], item.progress.get("repaired_observations", {}))
        targets: dict[str, list[str]] = {}
        for character in characters:
            bank = bank_id(character)
            current = await self._memory.observation_sources(bank)
            if set(repaired.get(character, [])) & current.keys():
                await asyncio.to_thread(
                    self._service.transition,
                    item.request_id,
                    "failed",
                    error="residue_remaining",
                )
                return "failed"
            removed = frozenset(snapshot[character]) - current_ids[character]
            affected = {
                identifier: (sources | frozenset(before[character].get(identifier, [])))
                for identifier, sources in current.items()
                if (sources | frozenset(before[character].get(identifier, []))) & removed
            }
            if not affected:
                continue
            targets[character] = sorted(affected)
            facts = current_ids[character] - current.keys()
            if any(not sources & facts for sources in affected.values()):
                # The public API cannot delete an orphan observation individually.
                # A bank clear requeues sources; source clears start auto consolidation.
                await self._memory.clear_observations(bank)
                targets[character] = sorted(current)
                surviving = facts
            else:
                surviving = frozenset[str]().union(*affected.values()) & facts
            for identifier in sorted(surviving):
                await self._memory.clear_memory_observations(bank, identifier)
        if not targets:
            return None
        progress = {**item.progress, "repaired_observations": {**repaired, **targets}}
        await asyncio.to_thread(self._store.save_progress, item, progress, self._clock())
        return "waiting"
