"""Request-owned synchronous read connections must be isolated and released."""

import asyncio
import importlib
import threading
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable

pytestmark = pytest.mark.ut
MODULE = "digital_souls_memory_hindsight.postgres_read_scope"


@pytest.mark.parametrize("fail", [False, True])
def test_scope_reuses_connection_and_closes_off_event_loop(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    scope = importlib.import_module(MODULE)
    connection = MagicMock(spec=psycopg.Connection)
    threads = []

    def connect(*args: object, **kwargs: object) -> MagicMock:
        threads.append(threading.get_ident())
        assert kwargs["autocommit"] is True
        return connection

    connection.close.side_effect = lambda: threads.append(threading.get_ident())
    connection.execute.side_effect = lambda *_: threads.append(threading.get_ident())
    factory = MagicMock(side_effect=connect)
    monkeypatch.setattr(psycopg, "connect", factory)

    async def exercise() -> None:
        event_loop_thread = threading.get_ident()

        def read() -> None:
            with scope.read_connection("synthetic-db") as active:
                active.execute("SELECT 1")

        async def request() -> None:
            async with scope.read_scope("synthetic-db"):
                await asyncio.to_thread(read)
                await asyncio.to_thread(read)
                if fail:
                    raise ValueError("synthetic request failure")

        if fail:
            with pytest.raises(ValueError, match="synthetic request failure"):
                await request()
        else:
            await request()
        assert factory.call_count == 1
        assert connection.execute.call_count == 2
        connection.close.assert_called_once()
        assert all(thread != event_loop_thread for thread in threads)
        # A later request must not reuse the connection released by this request.
        async with scope.read_scope("synthetic-db"):
            await asyncio.to_thread(read)
        assert factory.call_count == 2

    asyncio.run(exercise())


def test_cancelled_scope_waits_for_active_read_before_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = importlib.import_module(MODULE)
    connection = MagicMock(spec=psycopg.Connection)
    entered, release = threading.Event(), threading.Event()
    events: list[str] = []
    connection.close.side_effect = lambda: events.append("closed")
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))

    def read() -> None:
        with scope.read_connection("synthetic-db") as active:
            active.execute("SELECT 1")
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Test did not release active read")
            events.append("read-finished")

    async def exercise() -> None:
        async def request() -> None:
            async with scope.read_scope("synthetic-db"):
                await asyncio.to_thread(read)

        task = asyncio.create_task(request())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            # Cancellation cannot complete cleanup while the synchronous read owns the connection.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            connection.close.assert_not_called()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)

    asyncio.run(exercise())
    assert events == ["read-finished", "closed"]


def test_unused_scope_does_not_open_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = importlib.import_module(MODULE)
    factory = MagicMock()
    monkeypatch.setattr(psycopg, "connect", factory)

    async def exercise() -> None:
        async with scope.read_scope("synthetic-db"):
            await asyncio.sleep(0)

    asyncio.run(exercise())
    factory.assert_not_called()


def test_reads_outside_scope_keep_independent_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = importlib.import_module(MODULE)
    factory = MagicMock()
    monkeypatch.setattr(psycopg, "connect", factory)
    for _ in range(2):
        with scope.read_connection("synthetic-db") as connection:
            connection.execute("SELECT 1")
    assert factory.call_count == 2
    factory.assert_called_with("synthetic-db", connect_timeout=5)
    assert factory.return_value.__exit__.call_count == 2


def test_concurrent_scopes_use_separate_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = importlib.import_module(MODULE)
    connections = [MagicMock(spec=psycopg.Connection) for _ in range(2)]
    factory = MagicMock(side_effect=connections)
    monkeypatch.setattr(psycopg, "connect", factory)

    async def exercise() -> None:
        barrier = asyncio.Barrier(2)

        async def request() -> None:
            async with scope.read_scope("synthetic-db"):

                def read() -> None:
                    with scope.read_connection("synthetic-db") as connection:
                        connection.execute("SELECT 1")

                await asyncio.to_thread(read)
                await asyncio.wait_for(barrier.wait(), timeout=3)
                await asyncio.to_thread(read)

        await asyncio.gather(request(), request())

    asyncio.run(exercise())
    assert factory.call_count == 2
    for connection in connections:
        assert connection.execute.call_count == 2
        connection.close.assert_called_once()


def test_close_failure_is_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = importlib.import_module(MODULE)
    connection = MagicMock(spec=psycopg.Connection)
    connection.close.side_effect = psycopg.OperationalError("synthetic close failure")
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))

    async def exercise() -> None:
        with pytest.raises(EvidenceUnavailable):
            async with scope.read_scope("synthetic-db"):

                def read() -> None:
                    with scope.read_connection("synthetic-db") as active:
                        active.execute("SELECT 1")

                await asyncio.to_thread(read)

    asyncio.run(exercise())
