"""Trust remains external to consolidation and survives provision persistence."""

import asyncio
import os
import subprocess
import sys

import psycopg
import pytest
from hindsight_client import Hindsight
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory_hindsight.evidence.projection import bank_id
from tests.it1 import test_bank_projection as projection_tests
from tests.it1.test_bank_projection import TEA, Projection, hindsight_client
from tests.it1.test_recall_api import recall
from tests.it1.test_verbatim_anchors import anchors, register

pytestmark = pytest.mark.it1
projection = projection_tests.projection


async def wait_for_new_consolidation(client: Hindsight, bank: str, completed: int) -> int:
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        response = await client.operations.list_operations(
            bank_id=bank, type="consolidation", _request_timeout=5.0
        )
        operations = response.operations
        assert all(item.status not in {"failed", "cancelled"} for item in operations), (
            response.to_dict()
        )
        if len(operations) > completed and all(item.status == "completed" for item in operations):
            return len(operations)
        assert asyncio.get_running_loop().time() < deadline, response.to_dict()
        await asyncio.sleep(0.1)


def test_repeated_retains_and_consolidation_keep_memories_inferred(projection: Projection) -> None:
    p = projection
    character = p.characters[0]

    async def exercise() -> None:
        completed = 0
        async with hindsight_client() as client:
            for count in range(1, 4):
                p.post_turn(f"mention-{count}", text=TEA)
                if count == 1:
                    with anchors(p) as transport:
                        created = register(
                            transport, character, "tea-anchor", "mention-1", "commitment"
                        )
                        assert created.status_code == 201, created.text
                await p.drain(client)
                completed = await wait_for_new_consolidation(client, bank_id(character), completed)
                response = await recall(p, client, character, "翡翠茶")
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["memories"]
                assert any(memory["kind"] == "observation" for memory in body["memories"])
                assert all(memory["trust"] == "inferred" for memory in body["memories"])
                assert body["source_spans"]
                assert all(span["trust"] == "evidence" for span in body["source_spans"])
                assert body["verbatim_anchors"] == [
                    {
                        "anchor_id": "tea-anchor",
                        "turn_id": "mention-1",
                        "text": TEA,
                        "trust": "evidence",
                    }
                ]
                with psycopg.connect(
                    p.database, row_factory=dict_row, connect_timeout=5
                ) as connection:
                    row = connection.execute(
                        "SELECT * FROM provision_records WHERE provision_id=%s",
                        (body["provision_id"],),
                    ).fetchone()
                assert row is not None
                for column, items in (
                    ("memory_trust", "memories"),
                    ("source_span_trust", "source_spans"),
                    ("verbatim_anchor_trust", "verbatim_anchors"),
                ):
                    assert row[column] == [item["trust"] for item in body[items]]
                assert row["memory_ids"] == [memory["id"] for memory in body["memories"]]
                assert row["source_spans"] == [
                    {key: span[key] for key in ("turn_id", "start", "end")}
                    for span in body["source_spans"]
                ]
                assert row["verbatim_anchor_ids"] == ["tea-anchor"]

    asyncio.run(exercise())


def test_trust_migration_populates_existing_nonempty_and_empty_records(database: str) -> None:
    def migrate(operation: str, revision: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", operation, revision],
            env={**os.environ, "DATABASE_URL": database},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("downgrade", "0007_verbatim_anchors")
    with psycopg.connect(database, connect_timeout=5) as connection:
        for identifier, memories, spans, anchor_ids in (
            ("nonempty", ["m-2", "m-1"], [{"turn_id": "t-1", "start": 0, "end": 8}], ["a-1"]),
            ("empty", [], [], []),
        ):
            connection.execute(
                "INSERT INTO provision_records "
                "(provision_id, kind, character_id, session_id, surface, turn_id, "
                "memory_ids, source_spans, verbatim_anchor_ids, created_at) "
                "VALUES (%s, 'turn', 'synthetic-character', 'synthetic-session', 'Web', "
                "'t-1', %s, %s, %s, now())",
                (identifier, memories, Jsonb(spans), anchor_ids),
            )
    migrate("upgrade", "0008_provision_trust")
    with psycopg.connect(database, row_factory=dict_row, connect_timeout=5) as reader:
        rows = reader.execute("SELECT * FROM provision_records ORDER BY provision_id").fetchall()
    assert len(rows) == 2
    empty, nonempty = rows
    assert (
        empty["memory_trust"] == empty["source_span_trust"] == empty["verbatim_anchor_trust"] == []
    )
    assert nonempty["memory_trust"] == ["inferred", "inferred"]
    assert nonempty["source_span_trust"] == ["evidence"]
    assert nonempty["verbatim_anchor_trust"] == ["evidence"]
    assert nonempty["memory_ids"] == ["m-2", "m-1"]
    assert nonempty["source_spans"] == [{"turn_id": "t-1", "start": 0, "end": 8}]
    assert nonempty["verbatim_anchor_ids"] == ["a-1"]
