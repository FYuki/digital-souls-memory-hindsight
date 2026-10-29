"""Provision storage translates DB failures and rejects duplicate inserts."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable

pytestmark = pytest.mark.ut


def provision() -> object:
    ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
    return ports.ProvisionRecord(
        provision_id="synthetic-provision",
        kind="turn",
        character_id="char-a",
        session_id="s-1",
        surface="Web",
        query="架空のお茶 ' %s",
        turn_id=None,
        sensitivity="public",
        classifier_version="fixture-1",
        memory_ids=("z-memory", "a-memory"),
        source_spans=(ports.ProvisionSpan("t-1", 2, 8),),
        verbatim_anchor_ids=(),
        temperament_version=None,
        personality_version=None,
        created_at=datetime(2026, 10, 29, tzinfo=UTC),
    )


@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
def test_database_failures_escape_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_provision")
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = ("synthetic-provision",)
    if stage == "execute":
        connection.execute.side_effect = psycopg.OperationalError("synthetic execute failure")

    @contextmanager
    def connect(
        url: str, *, connect_timeout: int
    ) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        if stage == "connect":
            raise psycopg.OperationalError("synthetic connection failure")
        yield cast(psycopg.Connection[tuple[object, ...]], connection)
        if stage == "commit":
            raise psycopg.OperationalError("synthetic commit failure")

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(EvidenceUnavailable):
        module.PostgresProvisionStore("synthetic-db").add(provision())


def test_insert_without_returned_row_is_a_provision_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_provision")
    ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    with pytest.raises(ports.ProvisionConflict):
        module.PostgresProvisionStore("synthetic-db").add(provision())
