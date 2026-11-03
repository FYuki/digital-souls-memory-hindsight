"""Kind-specific resident record checks and shared provision identifiers."""

import importlib
import os
import subprocess
import sys
from unittest.mock import patch

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import Projection
from tests.it1.test_resident_context import assert_record, base, provide, residents
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.it1
projection = projection_tests.projection


def insert_record(database: str, **changes: object) -> None:
    row: dict[str, object] = {
        "provision_id": "resident-check",
        "kind": "resident",
        "character_id": "char-a",
        "session_id": "s-1",
        "surface": "Web",
        "query": None,
        "turn_id": None,
        "sensitivity": None,
        "classifier_version": None,
        "memory_ids": [],
        "source_spans": Jsonb([]),
        "verbatim_anchor_ids": [],
        "memory_trust": [],
        "source_span_trust": [],
        "verbatim_anchor_trust": [],
        "core_identity_version": None,
        "temperament_version": None,
        "personality_version": None,
        "exemplar_ids": [],
        "profile_revision": None,
        "profile_entries": Jsonb([]),
        "processing_time_ms": 0,
        "created_at": NOW,
        **changes,
    }
    with psycopg.connect(database, connect_timeout=5) as connection:
        connection.execute(
            sql.SQL("INSERT INTO provision_records ({}) VALUES ({})").format(
                sql.SQL(",").join(map(sql.Identifier, row)),
                sql.SQL(",").join(sql.Placeholder() for _ in row),
            ),
            tuple(row.values()),
        )


@pytest.mark.parametrize("query", [None, "架空のお茶"])
def test_resident_accepts_optional_query_without_a_turn(database: str, query: str | None) -> None:
    insert_record(
        database,
        query=query,
        sensitivity=None if query is None else "public",
        classifier_version=None if query is None else "fixture-1",
    )
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT kind, query, turn_id FROM provision_records"
        ).fetchone() == ("resident", query, None)


@pytest.mark.parametrize(
    "changes",
    [
        {"turn_id": "t-1"},
        {"exemplar_ids": None},
        {"profile_entries": None},
        {"profile_entries": Jsonb({})},
        {"processing_time_ms": None},
        {"processing_time_ms": -1},
        {"profile_entries": Jsonb([{"turn_id": "t-1", "start": 0, "end": 1}])},
        {"memory_ids": ["memory-1"], "memory_trust": ["inferred"]},
        {
            "source_spans": Jsonb([{"turn_id": "t-1", "start": 0, "end": 1}]),
            "source_span_trust": ["evidence"],
        },
        {"verbatim_anchor_ids": ["anchor-1"], "verbatim_anchor_trust": ["evidence"]},
    ],
)
def test_invalid_resident_shape_is_rejected_by_database(
    database: str, changes: dict[str, object]
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_record(database, **changes)


@pytest.mark.parametrize(
    "field, value",
    [
        ("core_identity_version", 1),
        ("temperament_version", 1),
        ("personality_version", 1),
        ("exemplar_ids", []),
        ("profile_revision", 1),
        ("profile_entries", Jsonb([])),
        ("processing_time_ms", 0),
    ],
)
def test_turn_cannot_store_resident_fields(database: str, field: str, value: object) -> None:
    fields: dict[str, object] = {
        "kind": "turn",
        "turn_id": "t-1",
        "exemplar_ids": None,
        "profile_entries": None,
        "processing_time_ms": None,
        field: value,
    }
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_record(database, **fields)


def test_upgrade_downgrade_roundtrip_preserves_turn_and_existing_constraints(database: str) -> None:
    def migrate(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env={**os.environ, "DATABASE_URL": database},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("downgrade", "0015_character_states")
    with psycopg.connect(database, connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO provision_records (provision_id,kind,character_id,session_id,surface,"
            "turn_id,memory_ids,source_spans,verbatim_anchor_ids,memory_trust,source_span_trust,"
            "verbatim_anchor_trust,created_at) VALUES ('old','turn','char-a','s-1','Web','t-1',"
            "'{}','[]','{}','{}','{}','{}',%s)",
            (NOW,),
        )
    for _ in range(2):
        migrate("upgrade", "head")
        with psycopg.connect(database, connect_timeout=5) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0016_resident_provisions",)
            ]
            assert connection.execute(
                "SELECT conname, count(*) FROM pg_constraint "
                "WHERE conrelid='provision_records'::regclass "
                "AND conname IN ('provision_kind','provision_turn_source') "
                "GROUP BY conname ORDER BY conname"
            ).fetchall() == [("provision_kind", 1), ("provision_turn_source", 1)]
            assert (
                connection.execute(
                    "SELECT temperament_version,personality_version,resident_provision_id,"
                    "core_identity_version,exemplar_ids,profile_revision,profile_entries,"
                    "processing_time_ms FROM provision_records "
                    "WHERE provision_id='old'"
                ).fetchone()
                == (None,) * 8
            )
        migrate("downgrade", "0015_character_states")
    migrate("upgrade", "head")


def existing_turn(p: Projection) -> None:
    insert_record(
        p.database,
        provision_id="turn-existing",
        kind="turn",
        character_id=p.characters[0],
        session_id=p.session,
        turn_id="query-turn",
        exemplar_ids=None,
        profile_entries=None,
        processing_time_ms=None,
    )


def test_fresh_resident_identifier_creates_one_record_without_changing_existing_turn(
    projection: Projection, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = projection
    existing_turn(p)
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        before = connection.execute(
            "SELECT * FROM provision_records WHERE provision_id='turn-existing'"
        ).fetchone()
    with residents(p, monkeypatch) as client:
        value = provide(client, p)
    assert value["provision_id"] != "turn-existing"
    assert_record(p, value)
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT count(*) FROM provision_records WHERE kind='resident'"
        ).fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT * FROM provision_records WHERE provision_id='turn-existing'"
            ).fetchone()
            == before
        )


def test_identifier_collision_with_turn_returns_409_without_overwriting_it(
    projection: Projection, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = projection
    existing_turn(p)
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        before = connection.execute("SELECT * FROM provision_records").fetchall()
    module = importlib.import_module("digital_souls_memory_hindsight.resident.service")
    with (
        residents(p, monkeypatch) as client,
        patch.object(module, "uuid4", return_value="turn-existing"),
    ):
        response = client.post(
            base(p) + "/resident", json={"session_id": p.session, "surface": "Web"}
        )
    assert response.status_code == 409, response.text
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute("SELECT * FROM provision_records").fetchall() == before
