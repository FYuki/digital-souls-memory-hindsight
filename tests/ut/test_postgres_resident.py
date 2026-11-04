"""Resident transaction isolation and persistence failure contracts."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.identity.ports import IdentitySnapshot
from digital_souls_memory_hindsight.recall.ports import ProvisionConflict
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


def record() -> object:
    ports = importlib.import_module("digital_souls_memory_hindsight.resident.ports")
    return ports.ResidentProvisionRecord(
        **{
            "provision_id": "resident-1",
            "character_id": "char-a",
            "session_id": "s-1",
            "surface": "Web",
            "query": None,
            "sensitivity": None,
            "classifier_version": None,
            "core_identity_version": None,
            "temperament_version": None,
            "personality_version": None,
            "exemplar_ids": (),
            "profile_revision": None,
            "profile_entries": (),
            "processing_time_ms": 0,
            "created_at": NOW,
        }
    )


def test_snapshot_uses_one_repeatable_read_connection_for_all_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_resident")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    connection.execute.return_value.fetchall.return_value = []
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    with module.PostgresResidentStore("synthetic-db").snapshot("char-a") as read:
        assert read.identity() == IdentitySnapshot(None, None, None)
        assert read.exemplars() == ()
        assert read.profile() is None
        assert read.turns_under_deletion(frozenset({"q"})) == frozenset()
        assert read.evidence.get_turn("q") is None
        assert read.evidence.get_delivery_scope("q") is None
        assert connection.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
    assert connect.call_count == 1


@pytest.mark.parametrize("operation", ["snapshot", "add"])
@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
def test_database_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, operation: str, stage: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_resident")
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = (
        None if operation == "snapshot" else ("resident-1",)
    )
    if stage == "execute":
        connection.execute.side_effect = psycopg.OperationalError("synthetic failure")

    @contextmanager
    def connect(
        url: str, *, connect_timeout: int, row_factory: object = None
    ) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        if stage == "connect":
            raise psycopg.OperationalError("synthetic failure")
        yield cast(psycopg.Connection[tuple[object, ...]], connection)
        if stage == "commit":
            raise psycopg.OperationalError("synthetic failure")

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(EvidenceUnavailable):
        if operation == "snapshot":
            with module.PostgresResidentStore("synthetic-db").snapshot("char-a") as read:
                read.identity()
        else:
            module.PostgresResidentStore("synthetic-db").add(record(), frozenset())


def test_duplicate_identifier_raises_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_resident")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    with pytest.raises(ProvisionConflict):
        module.PostgresResidentStore("synthetic-db").add(record(), frozenset())
