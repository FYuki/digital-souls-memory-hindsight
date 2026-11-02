"""Inspect active bank operations and page through memory identities using the SDK."""

from hindsight_client import Hindsight
from hindsight_client_api.exceptions import NotFoundException


class HindsightDeletion:
    def __init__(self, client: Hindsight) -> None:
        self._client = client

    async def bank_busy(self, bank_id: str) -> bool:
        try:
            for status in ("pending", "processing"):
                result = await self._client.operations.list_operations(
                    bank_id=bank_id, status=status, _request_timeout=10.0
                )
                if result.total:
                    return True
            return False
        except NotFoundException:
            return False

    async def memory_ids(self, bank_id: str) -> frozenset[str]:
        ids: set[str] = set()
        try:
            offset = 0
            while True:
                result = await self._client.memory.list_memories(
                    bank_id=bank_id, limit=100, offset=offset, _request_timeout=10.0
                )
                ids.update(str(item.id) for item in result.items)
                offset += len(result.items)
                if offset >= result.total:
                    return frozenset(ids)
                if not result.items:
                    raise RuntimeError("Memory enumeration did not advance")
        except NotFoundException:
            return frozenset()

    async def observation_sources(self, bank_id: str) -> dict[str, frozenset[str]]:
        sources: dict[str, frozenset[str]] = {}
        try:
            offset = 0
            while True:
                result = await self._client.memory.list_memories(
                    bank_id=bank_id,
                    type="observation",
                    limit=100,
                    offset=offset,
                    _request_timeout=10.0,
                )
                for item in result.items:
                    sources[item.id] = frozenset(item.source_memory_ids or [])
                offset += len(result.items)
                if offset >= result.total:
                    return sources
                if not result.items:
                    raise RuntimeError("Observation enumeration did not advance")
        except NotFoundException:
            return {}

    async def clear_memory_observations(self, bank_id: str, memory_id: str) -> None:
        await self._client.memory.clear_memory_observations(
            bank_id=bank_id,
            memory_id=memory_id,
            _request_timeout=10.0,
        )

    async def clear_observations(self, bank_id: str) -> None:
        await self._client.banks.clear_observations(bank_id=bank_id, _request_timeout=10.0)
