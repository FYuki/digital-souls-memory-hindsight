"""Deletion result and rebuild payloads at the persisted operation boundary."""

import asyncio
import importlib
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from digital_souls_memory_hindsight.deletion.domain import DeletionItem
from digital_souls_memory_hindsight.deletion.execution import DeletionExecutor
from digital_souls_memory_hindsight.deletion.ports import DeletionPersistence, MemoryIndex
from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt
from digital_souls_memory_hindsight.evidence.projection import bank_id
from tests.ut.test_deletion_http import DeletionStore
from tests.ut.test_delivery_scope import NOW, TEXT, Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore
from tests.ut.test_postgres_deletion import request

pytestmark = pytest.mark.ut


def test_cascade_result_contains_only_deleted_identifiers_and_document_targets() -> None:
    execution = importlib.import_module("digital_souls_memory_hindsight.deletion.execution")
    progress = {
        "ledger_done": True,
        "memory_snapshot": {"char-a": ["old-memory"]},
        "deleted_anchors": [
            {"character_id": "char-a", "anchor_id": "same-anchor"},
            {"character_id": "char-b", "anchor_id": "same-anchor"},
        ],
        "deleted_provision_ids": ["p-span", "p-memory"],
        "deleted_voice_exemplars": [{"character_id": "char-a", "exemplar_id": "voice-1"}],
        "rebuilt_documents": [{"bank_id": "bank-a", "document_id": "document"}],
        "deleted_documents": [{"bank_id": "bank-b", "document_id": "document"}],
        "content": "合成の削除本文",
        "query": "合成の問い合わせ",
    }
    assert execution.build_cascade_result(request(), progress) == {
        "deleted_turn_ids": ["t-1"],
        "rebuilt_documents": [{"bank_id": "bank-a", "document_id": "document"}],
        "deleted_documents": [{"bank_id": "bank-b", "document_id": "document"}],
        "deleted_anchors": [
            {"character_id": "char-a", "anchor_id": "same-anchor"},
            {"character_id": "char-b", "anchor_id": "same-anchor"},
        ],
        "deleted_provision_ids": ["p-span", "p-memory"],
        "deleted_voice_exemplars": [{"character_id": "char-a", "exemplar_id": "voice-1"}],
    }


def test_empty_deletion_cascade_has_empty_identifier_lists() -> None:
    execution = importlib.import_module("digital_souls_memory_hindsight.deletion.execution")
    progress = {
        "ledger_done": True,
        "memory_snapshot": {},
        "deleted_anchors": [],
        "deleted_voice_exemplars": [],
        "deleted_provision_ids": [],
        "rebuilt_documents": [],
        "deleted_documents": [],
    }
    assert execution.build_cascade_result(replace(request(), target_turn_ids=()), progress) == {
        "deleted_turn_ids": [],
        "rebuilt_documents": [],
        "deleted_documents": [],
        "deleted_anchors": [],
        "deleted_voice_exemplars": [],
        "deleted_provision_ids": [],
    }


@pytest.mark.parametrize(
    ("character", "expected"),
    [
        ("char-a", TEXT + "\n\n合成の観測。"),
        ("char-b", TEXT[:3] + "\n\n合成の観測。"),
        ("char-c", ""),
    ],
)
def test_rebuild_content_uses_remaining_turns_and_each_character_delivery_range(
    character: str, expected: str
) -> None:
    projection = importlib.import_module("digital_souls_memory_hindsight.evidence.projection")
    store = MemoryStore()
    service = service_with_session(store, Clock())
    service.add_turn(replace(draft(), turn_id="deleted", text="合成の削除マーカー"))
    service.add_turn(draft())
    service.add_receipt(DeliveryReceipt("t-1", "partial", 3, "Web", NOW))
    service.add_turn(
        replace(draft(), turn_id="observation", kind="observation", text="合成の観測。")
    )
    del store.turns["deleted"]
    store.tombstones.add("deleted")
    assert projection.document_content(store, "s-1", character) == expected


def test_rebuild_content_is_empty_after_the_last_turn_is_removed() -> None:
    projection = importlib.import_module("digital_souls_memory_hindsight.evidence.projection")
    store = MemoryStore()
    service = service_with_session(store, Clock(), reports_delivery=False)
    service.add_turn(draft())
    store.turns.clear()
    store.tombstones.add("t-1")
    for character in ("char-a", "char-b"):
        assert projection.document_content(store, "s-1", character) == ""


def execution_setup() -> tuple[DeletionService, MagicMock, MagicMock, DeletionItem]:
    evidence = service_with_session(MemoryStore(), Clock(), reports_delivery=False)
    evidence.add_turn(draft())
    service = DeletionService(DeletionStore(), evidence, clock=Clock())
    service.accept("d", "synthetic-author", ("t-1",), None)
    store = MagicMock(spec=DeletionPersistence)
    store.affected_pairs.return_value = (("char-a", "s-1"), ("char-b", "s-1"))
    store.projections_busy.return_value = False
    memory = MagicMock(spec=MemoryIndex)
    memory.bank_busy = AsyncMock(return_value=False)
    memory.memory_ids = AsyncMock(return_value=frozenset())
    memory.observation_sources = AsyncMock(return_value={})
    memory.clear_memory_observations = AsyncMock()
    memory.clear_observations = AsyncMock()
    item = DeletionItem(
        "d", "processing", 1, NOW, "lease", NOW + timedelta(seconds=60), None, None, {}
    )
    return service, store, memory, item


def completed_ledger_progress() -> dict[str, object]:
    return {
        "ledger_done": True,
        "memory_snapshot": {"char-a": ["old-a", "stay-a"], "char-b": ["old-b", "stay-b"]},
        "observation_sources": {"char-a": {}, "char-b": {}},
        "fenced_operations": [],
        "fenced_documents": [],
        "rebuilt_documents": [
            {"bank_id": bank_id("char-a"), "document_id": "document"},
            {"bank_id": bank_id("char-b"), "document_id": "document"},
        ],
        "deleted_documents": [],
        "deleted_anchors": [],
        "deleted_provision_ids": ["span"],
        "deleted_voice_exemplars": [],
    }


@pytest.mark.parametrize("busy_character", ["char-a", "char-b"])
def test_deletion_waits_for_every_affected_bank_before_touching_ledger(busy_character: str) -> None:
    service, store, memory, item = execution_setup()

    async def busy(bank: str) -> bool:
        return bank == bank_id(busy_character)

    memory.bank_busy.side_effect = busy
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == "waiting"
    )
    assert service.get("d").status == "processing"
    assert service.blocked_sessions(busy_character) == frozenset({"s-1"})
    store.delete_turns.assert_not_called()
    store.save_progress.assert_not_called()
    memory.memory_ids.assert_not_awaited()


@pytest.mark.parametrize("statuses", [(), ("pending", "completed"), ("processing", "completed")])
def test_deletion_does_not_finish_until_all_saved_rebuild_operations_complete(
    statuses: tuple[str, ...],
) -> None:
    service, store, memory, item = execution_setup()
    service.transition("d", "processing")
    item = replace(item, progress=completed_ledger_progress())
    store.rebuild_statuses.return_value = statuses
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == "waiting"
    )
    assert service.get("d").status == "processing"
    store.delete_provisions_for_memories.assert_not_called()
    store.delete_turns.assert_not_called()


def test_completed_rebuild_still_waits_for_consolidation() -> None:
    service, store, memory, item = execution_setup()
    service.transition("d", "processing")
    store.rebuild_statuses.return_value = ("completed", "completed")
    memory.bank_busy.return_value = True
    item = replace(item, progress=completed_ledger_progress())
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == "waiting"
    )
    assert service.get("d").status == "processing"
    store.delete_provisions_for_memories.assert_not_called()


def test_failed_rebuild_records_safe_failure_and_keeps_session_blocked() -> None:
    service, store, memory, item = execution_setup()
    service.transition("d", "processing")
    store.rebuild_statuses.return_value = ("completed", "failed")
    item = replace(item, progress=completed_ledger_progress())
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == "failed"
    )
    saved = service.get("d")
    assert (saved.status, saved.error, saved.completed_at) == ("failed", "rebuild_failed", None)
    assert service.blocked_sessions("char-a") == frozenset({"s-1"})
    store.delete_provisions_for_memories.assert_not_called()


def test_same_deletion_progress_resumes_without_repeating_ledger_or_expanding_memory_removal() -> (
    None
):
    service, store, memory, item = execution_setup()
    progress = completed_ledger_progress()
    store.delete_turns.return_value = progress
    store.rebuild_statuses.side_effect = [("pending", "pending"), ("completed", "completed")]
    memory.memory_ids.side_effect = [
        frozenset({"old-a", "stay-a"}),
        frozenset({"old-b", "stay-b"}),
        frozenset({"new-a", "stay-a"}),
        frozenset({"stay-b"}),
    ]

    def delete_provisions(
        current: DeletionItem, character: str, ids: frozenset[str], now: object
    ) -> dict[str, object]:
        assert ids == frozenset({"old-a" if character == "char-a" else "old-b"})
        return {**current.progress, "deleted_provision_ids": ["span", "memory"]}

    store.delete_provisions_for_memories.side_effect = delete_provisions
    executor = DeletionExecutor(service, store, memory, clock=Clock())

    async def exercise() -> None:
        assert await executor.step(item) == "waiting"
        assert service.get("d").status == "processing"
        assert await executor.step(replace(item, progress=progress)) == "completed"
        saved = service.get("d")
        assert saved.status == "completed" and saved.completed_at == NOW
        assert saved.cascade_result is not None
        assert saved.cascade_result["deleted_provision_ids"] == ["span", "memory"]
        assert service.blocked_sessions("char-a") == frozenset()
        assert await executor.step(replace(item, progress=progress)) == "completed"
        assert service.get("d") == saved

    asyncio.run(exercise())
    store.save_progress.assert_called_once()
    assert store.save_progress.call_args.args[1]["memory_snapshot"] == progress["memory_snapshot"]
    store.delete_turns.assert_called_once()
    assert store.delete_provisions_for_memories.call_count == 2
    assert memory.memory_ids.await_count == 4


def test_unconfigured_deletion_stays_accepted_and_does_not_delete() -> None:
    service, store, _, item = execution_setup()
    assert (
        asyncio.run(DeletionExecutor(service, store, None, clock=Clock()).step(item))
        == "not_configured"
    )
    assert service.get("d").status == "accepted"
    store.delete_turns.assert_not_called()


def test_deletion_persists_observation_sources_before_removing_ledger() -> None:
    service, store, memory, item = execution_setup()
    sources = {"mixed": frozenset({"old-a", "stay-a"})}
    memory.observation_sources.return_value = sources
    memory.memory_ids.return_value = frozenset({"old-a", "stay-a", "mixed"})
    store.rebuild_statuses.return_value = ("pending", "pending")

    def delete(current: DeletionItem, request: object, now: object) -> dict[str, object]:
        saved = store.save_progress.call_args.args[1]
        assert saved["observation_sources"] == {
            "char-a": {"mixed": ["old-a", "stay-a"]},
            "char-b": {"mixed": ["old-a", "stay-a"]},
        }
        assert current.progress == saved
        return {**completed_ledger_progress(), **saved, "ledger_done": True}

    store.delete_turns.side_effect = delete
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == "waiting"
    )
    store.save_progress.assert_called_once()


def test_active_append_lease_delays_snapshot_until_the_bank_can_be_deleted() -> None:
    service, store, memory, item = execution_setup()
    store.projections_busy = MagicMock(return_value=True)
    store.delete_turns.return_value = completed_ledger_progress()
    store.rebuild_statuses.return_value = ("pending", "pending")
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == "waiting"
    )
    memory.memory_ids.assert_not_awaited()
    memory.observation_sources.assert_not_awaited()
    store.save_progress.assert_not_called()
    store.delete_turns.assert_not_called()


@pytest.mark.parametrize("current_sources", [("stay-a",), ("old-a", "stay-a")])
def test_mixed_observation_is_rebuilt_before_completion_even_if_deleted_source_was_unlinked(
    current_sources: tuple[str, ...],
) -> None:
    service, store, memory, item = execution_setup()
    store.affected_pairs.return_value = (("char-a", "s-1"),)
    progress = completed_ledger_progress()
    progress["observation_sources"] = {
        "char-a": {"mixed": ["old-a", "stay-a"], "unrelated": ["other"]}
    }
    progress["memory_snapshot"] = {"char-a": ["old-a", "stay-a", "mixed", "unrelated", "other"]}
    progress["rebuilt_documents"] = [{"bank_id": bank_id("char-a"), "document_id": "document"}]
    store.rebuild_statuses.return_value = ("completed",)
    memory.memory_ids.return_value = frozenset({"stay-a", "mixed", "unrelated", "other"})
    memory.observation_sources.return_value = {
        "mixed": frozenset(current_sources),
        "unrelated": frozenset({"other"}),
    }
    item = replace(item, progress=progress)
    result = asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item))
    assert result == "waiting"
    assert service.get("d").status == "processing"
    memory.clear_memory_observations.assert_awaited_once_with(bank_id("char-a"), "stay-a")
    store.delete_provisions_for_memories.assert_not_called()
    store.delete_turns.assert_not_called()


def test_observations_with_only_surviving_sources_complete_without_rebuilding() -> None:
    service, store, memory, item = execution_setup()
    progress = completed_ledger_progress()
    progress["observation_sources"] = {"char-a": {"unrelated": ["stay-a"]}, "char-b": {}}
    store.rebuild_statuses.return_value = ("completed", "completed")
    memory.memory_ids.return_value = frozenset({"stay-a", "stay-b", "unrelated"})
    memory.observation_sources.return_value = {"unrelated": frozenset({"stay-a"})}
    store.delete_provisions_for_memories.side_effect = lambda current, *args: current.progress
    item = replace(item, progress=progress)
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item))
        == "completed"
    )
    assert service.get("d").status == "completed"
    memory.clear_memory_observations.assert_not_awaited()
    memory.clear_observations.assert_not_awaited()


def test_orphan_observation_clears_bank_and_rebuilds_surviving_facts() -> None:
    service, store, memory, item = execution_setup()
    store.affected_pairs.return_value = (("char-a", "s-1"),)
    progress = completed_ledger_progress()
    progress["memory_snapshot"] = {"char-a": ["old-a", "orphan", "stay-a", "unrelated"]}
    progress["observation_sources"] = {"char-a": {"orphan": ["old-a"], "unrelated": ["stay-a"]}}
    progress["rebuilt_documents"] = [{"bank_id": bank_id("char-a"), "document_id": "document"}]
    store.rebuild_statuses.return_value = ("completed",)
    memory.memory_ids.return_value = frozenset({"orphan", "stay-a", "unrelated"})
    memory.observation_sources.return_value = {
        "orphan": frozenset(),
        "unrelated": frozenset({"stay-a"}),
    }
    item = replace(item, progress=progress)
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == "waiting"
    )
    memory.clear_observations.assert_awaited_once_with(bank_id("char-a"))
    memory.clear_memory_observations.assert_awaited_once_with(bank_id("char-a"), "stay-a")
    assert service.get("d").status == "processing"


@pytest.mark.parametrize("residue_remains", [False, True])
def test_residue_repair_resumes_once_and_keeps_blocking_if_repair_failed(
    residue_remains: bool,
) -> None:
    service, store, memory, item = execution_setup()
    store.affected_pairs.return_value = (("char-a", "s-1"),)
    progress = completed_ledger_progress()
    progress["observation_sources"] = {"char-a": {"mixed": ["old-a", "stay-a"]}}
    progress["memory_snapshot"] = {"char-a": ["old-a", "stay-a", "mixed"]}
    progress["rebuilt_documents"] = [{"bank_id": bank_id("char-a"), "document_id": "document"}]
    item = replace(item, progress=progress)
    store.rebuild_statuses.return_value = ("completed",)
    memory.memory_ids.return_value = frozenset({"stay-a", "mixed"})
    memory.observation_sources.return_value = {"mixed": frozenset({"stay-a"})}
    executor = DeletionExecutor(service, store, memory, clock=Clock())

    async def exercise() -> None:
        assert await executor.step(item) == "waiting"
        saved = store.save_progress.call_args.args[1]
        resumed = replace(item, lease_token="new-lease", progress=saved)
        memory.bank_busy.return_value = True
        assert await executor.step(resumed) == "waiting"
        assert service.get("d").status == "processing"
        memory.bank_busy.return_value = False
        if not residue_remains:
            memory.memory_ids.return_value = frozenset({"stay-a", "rebuilt"})
            memory.observation_sources.return_value = {"rebuilt": frozenset({"stay-a"})}
        store.delete_provisions_for_memories.side_effect = lambda current, *args: current.progress
        assert await executor.step(resumed) == ("failed" if residue_remains else "completed")
        assert service.get("d").status == ("failed" if residue_remains else "completed")
        assert bool(service.blocked_sessions("char-a")) == residue_remains
        if residue_remains:
            store.delete_provisions_for_memories.assert_not_called()
        else:
            store.delete_provisions_for_memories.assert_called_once()
            assert store.delete_provisions_for_memories.call_args.args[2] == frozenset(
                {"old-a", "mixed"}
            )

    asyncio.run(exercise())
    memory.clear_memory_observations.assert_awaited_once_with(bank_id("char-a"), "stay-a")
    store.delete_turns.assert_not_called()


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (("completed", "completed"), "waiting"),
        (("completed", "completed", "completed", "pending"), "waiting"),
        (("completed", "completed", "completed", "failed"), "failed"),
        (("completed", "completed", "completed", "completed"), "completed"),
    ],
)
def test_deletion_waits_for_fences_and_temporary_document_cleanup(
    statuses: tuple[str, ...], expected: str
) -> None:
    service, store, memory, item = execution_setup()
    progress = {
        **completed_ledger_progress(),
        "fenced_operations": [{"operation_id": "old"}],
        "fenced_documents": [{"bank_id": "bank", "document_id": "temporary"}],
    }
    item = replace(item, progress=progress)
    store.rebuild_statuses.return_value = statuses
    store.delete_provisions_for_memories.side_effect = lambda current, *args: current.progress
    assert (
        asyncio.run(DeletionExecutor(service, store, memory, clock=Clock()).step(item)) == expected
    )
    if expected == "waiting":
        store.delete_provisions_for_memories.assert_not_called()
