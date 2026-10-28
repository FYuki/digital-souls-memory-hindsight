"""Disposable ledger databases for tests that drive their own worker or clock."""

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url


class ResponseGate:
    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def marker(self) -> str:
        return self.path.name

    async def wait_entered(self) -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while not list(self.path.glob("entered/*")):
            assert asyncio.get_running_loop().time() < deadline, "Stub did not receive marker"
            await asyncio.sleep(0.01)

    def release(self) -> None:
        (self.path / "release").touch()

    async def wait_finished(self) -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while {p.name for p in self.path.glob("entered/*")} != {
            p.name for p in self.path.glob("finished/*")
        }:
            assert asyncio.get_running_loop().time() < deadline, "Stub request did not finish"
            await asyncio.sleep(0.01)


@pytest.fixture
def response_gate() -> Iterator[ResponseGate]:
    path = Path(os.environ["STUB_CONTROL_DIR"]) / ("p1-" + uuid4().hex)
    path.mkdir()
    gate = ResponseGate(path)
    try:
        yield gate
    finally:
        gate.release()
        asyncio.run(gate.wait_finished())
        shutil.rmtree(path)


@pytest.fixture
def database() -> Iterator[str]:
    name = "outbox_" + uuid4().hex
    admin_url = os.environ["DATABASE_URL"]
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "DATABASE_URL": url},
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
