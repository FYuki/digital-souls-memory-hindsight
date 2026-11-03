"""Identity persistence converts connection, statement and commit failures."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import DictRow

from digital_souls_memory_hindsight.identity.ports import (
    CoreIdentity,
    IdentityConflict,
    IdentityUnavailable,
    PersonalityProfile,
    Temperament,
)
from digital_souls_memory_hindsight.postgres_identity import PostgresIdentityStore
from tests.ut.test_identity_http import seed_body

pytestmark = pytest.mark.ut
NOW = datetime(2026, 11, 3, tzinfo=UTC)
ASPECTS = cast(dict[str, float], seed_body()["aspects"])


@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
def test_identity_database_failure_becomes_unavailable(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    if stage == "execute":
        connection.execute.side_effect = psycopg.OperationalError("synthetic statement failure")

    @contextmanager
    def connect(
        url: str, *, row_factory: object, connect_timeout: int
    ) -> Iterator[psycopg.Connection[DictRow]]:
        if stage == "connect":
            raise psycopg.OperationalError("synthetic connect failure")
        yield cast(psycopg.Connection[DictRow], connection)
        if stage == "commit":
            raise psycopg.OperationalError("synthetic commit failure")

    monkeypatch.setattr(psycopg, "connect", connect)
    store = PostgresIdentityStore("synthetic-db")
    with pytest.raises(IdentityUnavailable):
        with store.transaction("char-a") as transaction:
            transaction.add_temperament(Temperament("char-a", 1, "穏やか", NOW))


def test_profile_unique_violation_becomes_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.side_effect = [MagicMock(), psycopg.errors.UniqueViolation()]
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    with pytest.raises(IdentityConflict):
        with PostgresIdentityStore("synthetic-db").transaction("char-a") as transaction:
            transaction.add_profile(
                PersonalityProfile("char-a", 1, ASPECTS, "架空の記述", "r1", 1, NOW)
            )


def test_identity_reads_restore_database_values(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    core_row = {
        "character_id": "char-a",
        "version": 2,
        "values": ["約束"],
        "prohibitions": ["攻撃"],
        "registered_at": NOW,
    }
    temperament_row = {
        "character_id": "char-a",
        "version": 3,
        "description": "穏やか",
        "registered_at": NOW,
    }
    profile_row = {
        "character_id": "char-a",
        "version": 1,
        "aspects": ASPECTS,
        "description": "架空の記述",
        "renderer_version": "r1",
        "temperament_version": 3,
        "registered_at": NOW,
    }
    connection.execute.return_value.fetchone.side_effect = [
        core_row,
        temperament_row,
        profile_row,
        core_row,
        temperament_row,
        profile_row,
        None,
        None,
        None,
    ]
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    store = PostgresIdentityStore("synthetic-db")
    expected_core = CoreIdentity("char-a", 2, ("約束",), ("攻撃",), NOW)
    expected_temperament = Temperament("char-a", 3, "穏やか", NOW)
    expected_profile = PersonalityProfile("char-a", 1, ASPECTS, "架空の記述", "r1", 3, NOW)
    current = store.current("char-a")
    assert current.core_identity == expected_core
    assert current.temperament == expected_temperament
    assert current.personality_profile == expected_profile
    assert store.get_core("char-a", 2) == expected_core
    assert store.get_temperament("char-a", 3) == expected_temperament
    assert store.get_profile("char-a", 1) == expected_profile
    assert store.get_core("char-a", 99) is None
    assert store.get_temperament("char-a", 99) is None
    assert store.get_profile("char-a", 99) is None


def test_identity_insert_transfers_registered_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    with PostgresIdentityStore("synthetic-db").transaction("char-a") as transaction:
        transaction.add_core(CoreIdentity("char-a", 2, ("約束",), (), NOW))
        transaction.add_temperament(Temperament("char-a", 3, "穏やか", NOW))
        transaction.add_profile(
            PersonalityProfile("char-a", 1, ASPECTS, "架空の記述", "r1", 3, NOW)
        )
    inserts = [call for call in connection.execute.call_args_list if isinstance(call.args[0], str)]
    assert inserts[-3].args[1] == ("char-a", 2, ["約束"], [], NOW)
    assert inserts[-2].args[1] == ("char-a", 3, "穏やか", NOW)
    profile = inserts[-1].args[1]
    assert profile[:2] == ("char-a", 1)
    assert profile[2].obj == ASPECTS
    assert profile[3:] == ("架空の記述", "r1", 3, NOW)
