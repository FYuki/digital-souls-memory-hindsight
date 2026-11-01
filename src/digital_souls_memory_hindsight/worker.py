"""One worker cycle shared by tests and the service lifespan."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .deletion.ports import DeletionStep
from .evidence.ports import EvidenceUnavailable
from .evidence.service import EvidenceService
from .outbox import OutboxStore, RetainExecutor

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Worker:
    def __init__(
        self,
        store: OutboxStore,
        executor: RetainExecutor | None,
        evidence: EvidenceService,
        *,
        clock: Callable[[], datetime] = _utc_now,
        deletion: DeletionStep | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._evidence = evidence
        self._clock = clock
        self._deletion = deletion

    async def run_once(self) -> None:
        await asyncio.to_thread(self._evidence.confirm_expired_deliveries)
        await self._retain_once()
        await self._deletion_once()

    async def _retain_once(self) -> None:
        item = await asyncio.to_thread(self._store.claim, self._clock(), timedelta(seconds=60))
        if item is None:
            return
        if self._executor is None:
            now = self._clock()
            await asyncio.to_thread(
                self._store.defer, item, now, now + timedelta(seconds=1), "not_configured"
            )
            return
        try:
            result = await self._executor.execute(item.operation)
        except Exception:
            # External exceptions can contain content or connection credentials.
            now = self._clock()
            await asyncio.to_thread(
                self._store.defer, item, now, now + timedelta(seconds=1), "transport_error"
            )
            return
        now = self._clock()
        if result.status == "completed":
            await asyncio.to_thread(self._store.complete, item, now)
        elif result.status == "failed":
            await asyncio.to_thread(self._store.fail, item, now, "retain_failed")
        else:
            await asyncio.to_thread(
                self._store.defer, item, now, now + timedelta(seconds=1), "pending"
            )

    async def _deletion_once(self) -> None:
        if self._deletion is None:
            return
        queue = self._deletion.queue
        item = await asyncio.to_thread(queue.claim, self._clock(), timedelta(seconds=60))
        if item is None:
            return
        try:
            result = await self._deletion.step(item)
        except Exception:
            now = self._clock()
            await asyncio.to_thread(
                queue.defer, item, now, now + timedelta(seconds=1), "transport_error"
            )
            return
        now = self._clock()
        if result == "completed":
            await asyncio.to_thread(queue.complete, item, now)
        elif result == "failed":
            await asyncio.to_thread(queue.fail, item, now, "rebuild_failed")
        else:
            await asyncio.to_thread(
                queue.defer,
                item,
                now,
                now + timedelta(seconds=1),
                "not_configured" if result == "not_configured" else "pending",
            )

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except EvidenceUnavailable:
                logger.warning("Worker storage cycle failed; retrying")
            except Exception as error:
                # The message can contain content or credentials; log only the type.
                logger.error("Worker cycle failed unexpectedly: %s", type(error).__name__)
            await asyncio.sleep(1)
