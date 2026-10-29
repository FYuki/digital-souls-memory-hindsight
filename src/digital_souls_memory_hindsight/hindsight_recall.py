"""Resolve recall evidence through the pinned official Hindsight client."""

import aiohttp
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ApiException, NotFoundException

from .recall.ports import RecalledFact, RecallUnavailable


class HindsightRecall:
    def __init__(self, client: Hindsight) -> None:
        self._client = client

    async def recall(self, bank_id: str, query: str) -> tuple[RecalledFact, ...]:
        try:
            response = await self._client.arecall(
                bank_id=bank_id,
                query=query,
                types=["world", "experience", "observation"],
                include_chunks=True,
                include_source_facts=True,
            )
        except NotFoundException:
            return ()
        except (ApiException, aiohttp.ClientError, TimeoutError):
            raise RecallUnavailable() from None
        chunks = response.chunks or {}
        source_facts = response.source_facts or {}
        facts: list[RecalledFact] = []
        for result in response.results:
            if result.type not in {"world", "experience", "observation"}:
                continue
            direct = result.document_id and result.chunk_id in chunks
            sources = (
                [result]
                if direct
                else [
                    source_facts[key] for key in result.source_fact_ids or () if key in source_facts
                ]
            )
            for source in sources:
                if not source.document_id or source.chunk_id not in chunks:
                    continue
                chunk = chunks[source.chunk_id]
                facts.append(
                    RecalledFact(
                        result.id, result.type, result.text, source.document_id, chunk.text
                    )
                )
        return tuple(facts)
