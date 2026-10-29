"""Worker recovery decisions and continuous delivery expiry with an empty outbox."""

import asyncio
import importlib
from dataclasses import replace
from datetime import timedelta
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.outbox import OutboxItem, RetainResult
from tests.ut.test_delivery_scope import NOW, Clock, draft, service_with_session
from tests.ut.test_evidence_http import MemoryStore

pytestmark = pytest.mark.ut


def test_same_worker_confirms_expired_deliveries_even_with_an_empty_outbox() -> None:
    worker_module = importlib.import_module("digital_souls_memory_hindsight.worker")
    clock = Clock()
    service = service_with_session(MemoryStore(), clock)
    saved = service.add_turn(draft())
    service.add_turn(replace(draft(), turn_id="received"))
    from digital_souls_memory_hindsight.evidence.domain import DeliveryReceipt

    service.add_receipt(DeliveryReceipt("received", "partial", 2, "Web", NOW))
    received = service.get_delivery_scope("received")
    queue = MagicMock()
    queue.claim.return_value = None
    worker = worker_module.Worker(queue, None, service, clock=clock)

    async def exercise() -> None:
        clock.now = NOW + timedelta(seconds=599)
        await worker.run_once()
        assert service.get_projection_text("t-1", "char-b") is None
        clock.now += timedelta(seconds=1)
        await worker.run_once()
        assert service.get_projection_text("t-1", "char-b") == saved.text
        assert service.get_delivery_scope("received") == received
        assert service.get_turn("t-1") == saved

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["completed", "pending", "failed", "transport_error"])
def test_worker_records_completion_pending_failure_and_retry_without_private_details(
    outcome: Literal["completed", "pending", "failed", "transport_error"],
) -> None:
    worker_module = importlib.import_module("digital_souls_memory_hindsight.worker")
    item = OutboxItem(
        "t-1",
        "bank",
        "session",
        "架空のお茶",
        NOW,
        "processing",
        1,
        NOW,
        "lease",
        NOW + timedelta(seconds=60),
        None,
        None,
    )
    queue = MagicMock()
    queue.claim.return_value = item
    executor = MagicMock()
    executor.execute = AsyncMock()
    if outcome == "transport_error":
        executor.execute.side_effect = TimeoutError("synthetic private detail")
    else:
        executor.execute.return_value = RetainResult(outcome)
    service = service_with_session(MemoryStore(), Clock())
    worker = worker_module.Worker(queue, executor, service, clock=Clock())
    asyncio.run(worker.run_once())
    executor.execute.assert_awaited_once_with(item.operation)
    if outcome == "completed":
        queue.complete.assert_called_once_with(item, NOW)
        queue.defer.assert_not_called()
        queue.fail.assert_not_called()
    elif outcome == "failed":
        queue.fail.assert_called_once_with(item, NOW, "retain_failed")
        queue.complete.assert_not_called()
    else:
        queue.defer.assert_called_once()
        arguments = queue.defer.call_args.args
        assert arguments[:3] == (item, NOW, NOW + timedelta(seconds=1))
        assert arguments[3] == outcome
        queue.complete.assert_not_called()


def test_unconfigured_retain_is_preserved_for_retry() -> None:
    worker_module = importlib.import_module("digital_souls_memory_hindsight.worker")
    queue = MagicMock()
    item = OutboxItem(
        "t-1",
        "bank",
        "session",
        "架空のお茶",
        NOW,
        "processing",
        1,
        NOW,
        "lease",
        NOW + timedelta(seconds=60),
        None,
        None,
    )
    queue.claim.return_value = item
    service = service_with_session(MemoryStore(), Clock())
    asyncio.run(worker_module.Worker(queue, None, service, clock=Clock()).run_once())
    queue.defer.assert_called_once_with(item, NOW, NOW + timedelta(seconds=1), "not_configured")
    queue.complete.assert_not_called()


def test_cancellation_leaves_claim_for_lease_recovery() -> None:
    worker_module = importlib.import_module("digital_souls_memory_hindsight.worker")
    queue = MagicMock()
    item = OutboxItem(
        "t-1",
        "bank",
        "session",
        "架空のお茶",
        NOW,
        "processing",
        1,
        NOW,
        "lease",
        NOW + timedelta(seconds=60),
        None,
        None,
    )
    queue.claim.return_value = item
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=asyncio.CancelledError)
    service = service_with_session(MemoryStore(), Clock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_module.Worker(queue, executor, service, clock=Clock()).run_once())
    queue.complete.assert_not_called()
    queue.fail.assert_not_called()
    queue.defer.assert_not_called()


def test_periodic_worker_recovers_after_a_temporary_storage_failure() -> None:
    worker_module = importlib.import_module("digital_souls_memory_hindsight.worker")
    service = service_with_session(MemoryStore(), Clock())

    async def exercise() -> None:
        queue = MagicMock()
        recovered = asyncio.Event()
        loop = asyncio.get_running_loop()
        attempts = 0

        def claim(now: object, lease_for: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise EvidenceUnavailable()
            loop.call_soon_threadsafe(recovered.set)

        queue.claim.side_effect = claim
        worker = worker_module.Worker(queue, None, service, clock=Clock())
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(recovered.wait(), timeout=3)
            assert attempts >= 2
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


def test_periodic_worker_survives_an_unexpected_error_without_logging_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker_module = importlib.import_module("digital_souls_memory_hindsight.worker")
    service = service_with_session(MemoryStore(), Clock())

    async def exercise() -> None:
        queue = MagicMock()
        recovered = asyncio.Event()
        loop = asyncio.get_running_loop()
        attempts = 0

        def claim(now: object, lease_for: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic-secret-text")
            loop.call_soon_threadsafe(recovered.set)

        queue.claim.side_effect = claim
        worker = worker_module.Worker(queue, None, service, clock=Clock())
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(recovered.wait(), timeout=3)
            assert attempts >= 2
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    with caplog.at_level("ERROR", logger="digital_souls_memory_hindsight.worker"):
        asyncio.run(exercise())
    assert "RuntimeError" in caplog.text
    assert "synthetic-secret-text" not in caplog.text
