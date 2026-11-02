"""The application lifespan owns its worker task and official SDK client."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.ut


def test_lifespan_runs_worker_and_closes_client_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight:8888")
    module = importlib.reload(importlib.import_module("digital_souls_memory_hindsight.app"))
    client = MagicMock()
    client.aclose = AsyncMock()
    monkeypatch.setattr(module, "Hindsight", MagicMock(return_value=client))
    index_factory = MagicMock(wraps=module.HindsightDeletion)
    monkeypatch.setattr(module, "HindsightDeletion", index_factory)
    worker = MagicMock()
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def run() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    worker.run = run
    worker_factory = MagicMock(return_value=worker)
    monkeypatch.setattr(module, "Worker", worker_factory)

    async def exercise() -> None:
        async with module.app.router.lifespan_context(module.app):
            await asyncio.wait_for(entered.wait(), timeout=1)
            client.aclose.assert_not_awaited()
            deletion = worker_factory.call_args.kwargs["deletion"]
            assert isinstance(deletion, module.DeletionExecutor)
            assert isinstance(deletion.queue, module.PostgresDeletionStore)
            index_factory.assert_called_once_with(client)
        assert stopped.is_set()
        client.aclose.assert_awaited_once()

    asyncio.run(exercise())
