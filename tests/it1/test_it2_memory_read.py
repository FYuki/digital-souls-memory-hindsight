"""Confirm scoring input lineage against the pinned server and fixture models."""

import asyncio
from importlib import import_module

import pytest

from tests.it1.test_bank_projection import (
    TEA,
    Projection,
    hindsight_client,
    projection,
)
from tests.it2.completion import evaluate_completion, read_completion_snapshot

pytestmark = pytest.mark.it1
__all__ = ["projection"]


def test_quality_reader_preserves_document_and_observation_sources(projection: Projection) -> None:
    quality_run = import_module("tests.it2.quality_run")
    projection.post_turn("quality-seed", text=TEA)
    bank, document = projection.target("quality-seed")

    async def exercise() -> None:
        async with hindsight_client() as client:
            await projection.drain(client)
            deadline = asyncio.get_running_loop().time() + 60
            while True:
                completion = evaluate_completion(
                    **await read_completion_snapshot(client, projection.database, bank)
                )
                assert completion.status != "failed", completion.reason
                if completion.status == "complete":
                    break
                assert asyncio.get_running_loop().time() < deadline, completion.reason
                await asyncio.sleep(0.1)
            rows = await quality_run.read_memories(client, bank)
            facts = {row["id"]: row for row in rows if row["fact_type"] != "observation"}
            observations = [row for row in rows if row["fact_type"] == "observation"]
            assert facts
            assert all(row["document_id"] == document for row in facts.values())
            assert observations
            assert any(set(row["source_memory_ids"]) & facts.keys() for row in observations)

    asyncio.run(exercise())
