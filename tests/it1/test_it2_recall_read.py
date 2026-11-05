"""Pinned SDK score recording agrees with service filtering using fixture models."""

import asyncio
from importlib import import_module

import pytest

from digital_souls_memory_hindsight.hindsight_recall import HindsightRecall
from tests.it1.test_bank_projection import (
    TEA,
    Projection,
    hindsight_client,
    projection,
)
from tests.it2.completion import evaluate_completion, read_completion_snapshot

pytestmark = pytest.mark.it1
__all__ = ["projection"]


def test_recorded_candidates_keep_server_scores_and_resolvable_sources(
    projection: Projection,
) -> None:
    abstention = import_module("tests.it2.abstention_run")
    threshold = import_module("tests.it2.threshold")
    projection.post_turn("recall-score-seed", text=TEA)
    bank, document = projection.target("recall-score-seed")

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
            candidates = await abstention.read_candidates(client, bank, TEA)
            assert candidates
            assert all(isinstance(row["reranker_score"], (int, float)) for row in candidates)
            assert any(
                source["document_id"] == document for row in candidates for source in row["sources"]
            )
            kept = threshold.filter_candidates(candidates, threshold=0.5)
            facts = await HindsightRecall(client, min_reranker_score=0.5).recall(bank, TEA)
            assert [row["id"] for row in kept] == [fact.id for fact in facts]

    asyncio.run(exercise())
