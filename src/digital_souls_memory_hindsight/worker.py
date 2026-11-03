"""One worker cycle shared by tests and the service lifespan."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from .character_state.generator import CharacterStateGenerator
from .deletion.domain import DeletionItem, DeletionLeaseLost
from .deletion.ports import DeletionQueue, DeletionStep, DeletionStepResult
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
        state: CharacterStateGenerator | None = None,
        deletion_lease: timedelta = timedelta(seconds=60),
        deletion_heartbeat: timedelta = timedelta(seconds=20),
    ) -> None:
        if deletion_heartbeat <= timedelta(0) or deletion_heartbeat >= deletion_lease:
            raise ValueError("Heartbeat must be positive and shorter than the lease")
        self._store = store
        self._executor = executor
        self._evidence = evidence
        self._clock = clock
        self._deletion = deletion
        self._state = state
        self._deletion_lease = deletion_lease
        self._deletion_heartbeat = deletion_heartbeat

    async def run_once(self) -> None:
        await asyncio.to_thread(self._evidence.confirm_expired_deliveries)
        await self._retain_once()
        await self._deletion_once()
        if self._state is not None:
            await self._state.run_once()

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
            if not await asyncio.to_thread(self._store.owns, item, self._clock()):
                return
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
        item = await asyncio.to_thread(queue.claim, self._clock(), self._deletion_lease)
        if item is None:
            return
        try:
            result = await self._step_with_heartbeat(queue, item)
        except DeletionLeaseLost:
            now = self._clock()
            # Usually rejected because the lease is gone; harmless when it is.
            await asyncio.to_thread(
                queue.defer, item, now, now + timedelta(seconds=1), "lease_lost"
            )
            return
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

    async def _step_with_heartbeat(
        self, queue: DeletionQueue, item: DeletionItem
    ) -> DeletionStepResult:
        """Run one deletion step while renewing its lease; stop the step if renewal fails."""
        assert self._deletion is not None
        step = asyncio.ensure_future(self._deletion.step(item))
        interval = self._deletion_heartbeat.total_seconds()
        try:
            while True:
                done, _ = await asyncio.wait({step}, timeout=interval)
                if done:
                    return step.result()
                try:
                    renewed = await asyncio.to_thread(
                        queue.renew, item, self._clock(), self._deletion_lease
                    )
                except Exception as error:
                    # The message can contain connection details; log only the type.
                    logger.warning("Deletion lease renewal failed: %s", type(error).__name__)
                    renewed = False
                if not renewed:
                    raise DeletionLeaseLost()
        finally:
            if not step.done():
                step.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await step

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
