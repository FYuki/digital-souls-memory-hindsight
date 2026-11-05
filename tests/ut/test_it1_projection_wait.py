"""An upstream append retry delayed by sixty seconds still reaches the IT1 assertions."""

import asyncio
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.ut.test_delivery_scope import Clock

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("completes", [True, False])
def test_projection_wait_covers_delayed_retry_and_remains_bounded(
    monkeypatch: pytest.MonkeyPatch, completes: bool
) -> None:
    module = import_module("tests.it1.test_bank_projection")
    elapsed = [0.0]
    worker = MagicMock()
    worker.run_once = AsyncMock()
    monkeypatch.setattr(module, "Worker", MagicMock(return_value=worker))
    connection = MagicMock()
    connection.execute.return_value.__iter__.side_effect = lambda: iter(
        [("completed" if completes and elapsed[0] >= 61 else "pending",)]
    )
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(module.psycopg, "connect", connect)

    async def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    monkeypatch.setattr(
        module,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: elapsed[0]), sleep=sleep
        ),
    )
    projection = module.Projection(
        database="synthetic-db",
        service=MagicMock(),
        http=MagicMock(),
        clock=Clock(),
        characters=("a", "b", "c"),
        session="synthetic-session",
    )
    if completes:
        asyncio.run(projection.drain(MagicMock()))
        assert 61 <= elapsed[0] < 62
    else:
        with pytest.raises(AssertionError):
            asyncio.run(projection.drain(MagicMock()))
        assert 120 <= elapsed[0] < 121
