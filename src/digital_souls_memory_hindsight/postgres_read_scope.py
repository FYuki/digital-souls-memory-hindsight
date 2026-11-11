"""Lazy, request-owned synchronous connections for recall reads."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from threading import Lock

import psycopg

from .evidence.ports import EvidenceUnavailable


class _ReadScope:
    def __init__(self, url: str) -> None:
        self.url = url
        self.connection: psycopg.Connection[tuple[object, ...]] | None = None
        self.closed = False
        self.lock = Lock()

    @contextmanager
    def read(self) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        # A cancelled to_thread call can still be reading when scope cleanup starts.
        with self.lock:
            if self.closed:
                raise psycopg.OperationalError("Read scope is closed")
            if self.connection is None:
                self.connection = psycopg.connect(self.url, connect_timeout=5, autocommit=True)
            yield self.connection

    def close(self) -> None:
        with self.lock:
            self.closed = True
            if self.connection is not None:
                self.connection.close()


_current_scope: ContextVar[_ReadScope | None] = ContextVar("recall_read_scope", default=None)


@asynccontextmanager
async def read_scope(url: str) -> AsyncIterator[None]:
    scope = _ReadScope(url)
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)
        try:
            await asyncio.to_thread(scope.close)
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error


@contextmanager
def read_connection(url: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    scope = _current_scope.get()
    if scope is not None and scope.url == url:
        with scope.read() as connection:
            yield connection
    else:
        with psycopg.connect(url, connect_timeout=5) as connection:
            yield connection
