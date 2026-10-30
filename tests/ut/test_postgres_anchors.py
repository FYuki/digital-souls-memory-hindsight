"""Anchor persistence failures and duplicate arbitration at the database boundary."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.anchors.ports import VerbatimAnchor
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


def anchor() -> VerbatimAnchor:
    return VerbatimAnchor(
        character_id="char-a",
        anchor_id="anchor-1",
        turn_id="t-1",
        kind="commitment",
        reason="架空の約束",
        approved_by="synthetic-author",
        registered_at=NOW,
    )


@pytest.mark.parametrize("operation", ["add", "get", "list", "match"])
@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
def test_database_failures_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, operation: str, stage: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = ("anchor-1",)
    connection.execute.return_value.fetchall.return_value = []
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
    store = module.PostgresAnchorStore("synthetic-db")
    with pytest.raises(EvidenceUnavailable):
        if operation == "add":
            store.add(anchor())
        elif operation == "get":
            connection.execute.return_value.fetchone.return_value = None
            store.get("char-a", "anchor-1")
        elif operation == "list":
            store.list("char-a")
        else:
            store.match((("anchor-1", "架空の約束"),), "約束")


def test_insert_without_returned_row_is_anchor_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    ports = importlib.import_module("digital_souls_memory_hindsight.anchors.ports")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    with pytest.raises(ports.AnchorConflict):
        module.PostgresAnchorStore("synthetic-db").add(anchor())


def test_match_transfers_projected_candidates_and_query_as_sql_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = [("anchor-1",)]
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    text = "架空の約束 ' %s"
    query = "約束 ' OR ( %s"
    result = module.PostgresAnchorStore("synthetic-db").match((("anchor-1", text),), query)
    assert result == frozenset({"anchor-1"})
    sql, parameters = connection.execute.call_args.args
    assert text not in str(sql)
    assert query not in str(sql)
    assert parameters == (["anchor-1"], [text], query)


@pytest.mark.parametrize("operation", ["get", "list"])
def test_reads_restore_anchor_metadata_from_database_rows(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_anchors")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    row = ("char-a", "anchor-1", "t-1", "commitment", "架空の約束", "synthetic-author", NOW)
    connection.execute.return_value.fetchone.return_value = row
    connection.execute.return_value.fetchall.return_value = [row]
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    store = module.PostgresAnchorStore("synthetic-db")
    if operation == "get":
        assert store.get("char-a", "anchor-1") == anchor()
    else:
        assert store.list("char-a") == (anchor(),)
