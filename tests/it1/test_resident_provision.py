"""Resident references in turn recall records and the author deletion workflow."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.evidence.projection import bank_id
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import TEA, Projection, hindsight_client, wait_idle
from tests.it1.test_deletion_execution import finish_deletion
from tests.it1.test_recall_api import recall

pytestmark = pytest.mark.it1
projection = projection_tests.projection


def test_upgrade_keeps_historical_turn_identity_versions_and_resident_reference_null(
    database: str,
) -> None:
    def migrate(*arguments: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            env={**os.environ, "DATABASE_URL": database},
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("downgrade", "0011_deletion_fencing")
    with psycopg.connect(database, connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO provision_records (provision_id, kind, character_id, session_id, "
            "surface, turn_id, memory_ids, source_spans, verbatim_anchor_ids, "
            "memory_trust, source_span_trust, verbatim_anchor_trust, created_at) "
            "VALUES ('historical','turn','char-a','s-1','Web','t-1','{}','[]','{}',"
            "'{}','{}','{}',%s)",
            (datetime(2026, 11, 1, tzinfo=UTC),),
        )
    migrate("upgrade", "head")
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT resident_provision_id, temperament_version, personality_version "
            "FROM provision_records WHERE provision_id='historical'"
        ).fetchone() == (None, None, None)


def add_resident(
    p: Projection,
    identifier: str,
    *,
    kind: str = "resident",
    character: str | None = None,
    session: str | None = None,
    source_turn: str | None = None,
) -> None:
    spans = [] if source_turn is None else [{"turn_id": source_turn, "start": 0, "end": len(TEA)}]
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO provision_records (provision_id, kind, character_id, session_id, "
            "surface, turn_id, memory_ids, source_spans, verbatim_anchor_ids, "
            "memory_trust, source_span_trust, verbatim_anchor_trust, created_at) "
            "VALUES (%s,%s,%s,%s,'Web',%s,'{}',%s,'{}','{}',%s,'{}',%s)",
            (
                identifier,
                kind,
                p.characters[0] if character is None else character,
                p.session if session is None else session,
                "synthetic-query-turn" if kind == "turn" else None,
                Jsonb(spans),
                ["evidence"] if spans else [],
                p.clock.now,
            ),
        )


@pytest.mark.parametrize("use_turn", [False, True])
def test_recall_saves_matching_resident_reference_and_keeps_identity_versions_null(
    projection: Projection,
    use_turn: bool,
) -> None:
    p = projection
    p.post_turn("tea", text=TEA)
    add_resident(p, "resident-1")

    async def exercise() -> str:
        async with hindsight_client() as client:
            await p.drain(client)
            await wait_idle(client, bank_id(p.characters[0]))
            response = await recall(
                p,
                client,
                p.characters[0],
                "翡翠茶",
                resident_provision_id="resident-1",
                query=None if use_turn else "翡翠茶",
                turn_id="tea" if use_turn else None,
            )
            assert response.status_code == 200, response.text
            assert response.json()["temperament_version"] is None
            assert response.json()["personality_version"] is None
            assert "resident_provision_id" not in response.json()
            return str(response.json()["provision_id"])

    identifier = asyncio.run(exercise())
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT resident_provision_id, temperament_version, personality_version "
            "FROM provision_records WHERE provision_id=%s",
            (identifier,),
        ).fetchone() == ("resident-1", None, None)


@pytest.mark.parametrize("invalid", ["missing", "turn", "character", "session"])
def test_recall_rejects_nonmatching_resident_without_creating_a_record(
    projection: Projection,
    invalid: str,
) -> None:
    p = projection
    p.post_turn("tea", text=TEA)
    if invalid != "missing":
        add_resident(
            p,
            "invalid-ref",
            kind="turn" if invalid == "turn" else "resident",
            character=p.characters[1] if invalid == "character" else p.characters[0],
            session="other-session" if invalid == "session" else p.session,
        )
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        before = connection.execute(
            "SELECT * FROM provision_records ORDER BY provision_id"
        ).fetchall()

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            await wait_idle(client, bank_id(p.characters[0]))
            response = await recall(
                p,
                client,
                p.characters[0],
                "翡翠茶",
                resident_provision_id="invalid-ref",
            )
            assert response.status_code == 422, response.text

    asyncio.run(exercise())
    with psycopg.connect(p.database, connect_timeout=5) as connection:
        assert (
            connection.execute("SELECT * FROM provision_records ORDER BY provision_id").fetchall()
            == before
        )


def test_author_deletion_nulls_reference_without_deleting_referring_turn_record(
    projection: Projection,
) -> None:
    p = projection
    p.post_turn("target", text=TEA)
    add_resident(p, "resident-source", source_turn="target")
    store = PostgresDeletionStore(p.database)
    service = DeletionService(store, p.service, clock=p.clock)

    async def exercise() -> None:
        async with hindsight_client() as client:
            await p.drain(client)
            await wait_idle(client, bank_id(p.characters[0]))
            # An unrelated query creates a record with no source span being deleted.
            response = await recall(
                p,
                client,
                p.characters[0],
                "コーヒー",
                min_reranker_score=0.5,
                resident_provision_id="resident-source",
            )
            assert response.status_code == 200, response.text
            assert response.json()["source_spans"] == []
            referring = response.json()["provision_id"]
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                assert connection.execute(
                    "SELECT resident_provision_id FROM provision_records WHERE provision_id=%s",
                    (referring,),
                ).fetchone() == ("resident-source",)
            service.accept("delete-resident", "synthetic-author", ("target",), None)
            await finish_deletion(p, client, service, store, "delete-resident")
            result = service.get("delete-resident").cascade_result
            assert result is not None
            assert set(result) == {
                "deleted_turn_ids",
                "rebuilt_documents",
                "deleted_documents",
                "deleted_anchors",
                "deleted_provision_ids",
            }
            deleted = result["deleted_provision_ids"]
            assert isinstance(deleted, list)
            assert "resident-source" in deleted
            assert referring not in deleted
            with psycopg.connect(p.database, connect_timeout=5) as connection:
                assert connection.execute(
                    "SELECT resident_provision_id FROM provision_records WHERE provision_id=%s",
                    (referring,),
                ).fetchone() == (None,)
                assert connection.execute(
                    "SELECT count(*) FROM provision_records WHERE provision_id='resident-source'"
                ).fetchone() == (0,)

    asyncio.run(exercise())
