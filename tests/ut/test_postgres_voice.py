"""Voice persistence reports database failure rather than an empty success."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.voice.domain import Span, VoiceExemplar
from digital_souls_memory_hindsight.voice.ports import VoiceConflict
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("operation", ["add", "get", "list"])
@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
def test_database_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, operation: str, stage: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_voice")
    domain = importlib.import_module("digital_souls_memory_hindsight.voice.domain")
    connection = MagicMock(spec=psycopg.Connection)
    connection.execute.return_value.fetchone.return_value = ("voice-1",)
    connection.execute.return_value.fetchall.return_value = []
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    connection.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    if stage == "execute":
        connection.execute.side_effect = psycopg.OperationalError("synthetic failure")
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = (
            psycopg.OperationalError("synthetic failure")
        )

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
    store = module.PostgresVoiceStore("synthetic-db")
    with pytest.raises(EvidenceUnavailable):
        if operation == "add":
            store.add(
                domain.VoiceExemplar(
                    character_id="char-a",
                    exemplar_id="voice-1",
                    source="canonical_seed",
                    answer="架空の回答",
                    question=None,
                    answer_span=None,
                    question_span=None,
                    approved_by="synthetic-author",
                    registered_at=NOW,
                )
            )
        elif operation == "get":
            connection.execute.return_value.fetchone.return_value = None
            store.get("char-a", "voice-1")
        else:
            store.list("char-a")


@pytest.mark.parametrize("operation", ["get", "list"])
@pytest.mark.parametrize("source", ["canonical_seed", "turn"])
def test_reads_restore_verbatim_text_and_optional_spans(
    monkeypatch: pytest.MonkeyPatch, operation: str, source: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_voice")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    a = Span("a", 1, 4) if source == "turn" else None
    q = Span("q", 0, 3) if source == "turn" else None
    expected = VoiceExemplar("char-a", "voice-1", source, "😀b🍵", " Ｑ\n", a, q, "作者", NOW)
    row = (
        "char-a",
        "voice-1",
        source,
        "😀b🍵",
        " Ｑ\n",
        "a" if a else None,
        1 if a else None,
        4 if a else None,
        "q" if q else None,
        0 if q else None,
        3 if q else None,
        "作者",
        NOW,
    )
    connection.execute.return_value.fetchone.return_value = row
    connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [row]
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    store = module.PostgresVoiceStore("synthetic-db")
    if operation == "get":
        assert store.get("char-a", "voice-1") == expected
    else:
        assert store.list("char-a") == (expected,)


def test_duplicate_insert_is_voice_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_voice")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    value = VoiceExemplar("char-a", "v", "canonical_seed", "回答", None, None, None, "作者", NOW)
    with pytest.raises(VoiceConflict):
        module.PostgresVoiceStore("synthetic-db").add(value)
