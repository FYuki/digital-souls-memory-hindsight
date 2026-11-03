"""Database errors remain failures at the profile persistence boundary."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.voice.domain import Span
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("operation", ["save", "get"])
@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
def test_database_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, operation: str, stage: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_character_profile")
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = (1,)
    connection.execute.return_value.fetchall.return_value = []
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    cursor.fetchall.return_value = []
    if stage == "execute":
        connection.execute.side_effect = psycopg.OperationalError("synthetic failure")
        cursor.execute.side_effect = psycopg.OperationalError("synthetic failure")

    @contextmanager
    def connect(
        url: str, *, connect_timeout: int
    ) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        if stage == "connect":
            raise psycopg.OperationalError("synthetic failure")
        yield cast(psycopg.Connection[tuple[object, ...]], connection)
        if stage == "commit":
            raise psycopg.OperationalError("synthetic failure")

    monkeypatch.setattr(psycopg, "connect", connect)
    store = module.PostgresCharacterProfileStore("synthetic-db")
    with pytest.raises(EvidenceUnavailable):
        if operation == "save":
            store.save("char-a", 0, (), NOW)
        else:
            connection.execute.return_value.fetchone.return_value = None
            cursor.fetchone.return_value = None
            store.get("char-a")


@pytest.mark.parametrize("condition", ["missing", "tombstone", "accepted"])
def test_save_rechecks_deleted_sources_before_replacing_existing_profile(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_character_profile")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_profile.domain")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection

    def execute(sql: str, parameters: object = None) -> MagicMock:
        result = MagicMock()
        result.fetchall.return_value = []
        if "session_id FROM turns" in sql:
            result.fetchall.return_value = [("s-1",)]
        elif "FOR KEY SHARE" in sql:
            result.fetchall.return_value = [] if condition == "missing" else [("target",)]
        elif (condition == "tombstone" and "turn_tombstones" in sql) or (
            condition == "accepted" and "deletion_requests" in sql
        ):
            result.fetchall.return_value = [("target",)]
        return result

    connection.execute.side_effect = execute
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    evidence = MagicMock()
    monkeypatch.setattr(module, "PostgresEvidenceStore", MagicMock(return_value=evidence))
    with pytest.raises(domain.ProfileValidationError):
        module.PostgresCharacterProfileStore("synthetic-db").save(
            "char-a", 2, (Span("target", 0, 2),), NOW
        )


@pytest.mark.parametrize("entries", [(), (Span("q", 1, 4), Span("a", 0, 2))])
def test_get_restores_profile_revision_and_ordered_original_intervals(
    monkeypatch: pytest.MonkeyPatch, entries: tuple[Span, ...]
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_character_profile")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_profile.domain")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = ("char-a", 5, 7, NOW)
    connection.execute.return_value.fetchall.return_value = [
        (entry.turn_id, entry.start, entry.end) for entry in entries
    ]
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    assert module.PostgresCharacterProfileStore("synthetic-db").get("char-a") == (
        domain.CharacterProfile("char-a", 5, 7, entries, NOW)
    )
