"""SDK operation filters and paginated identity enumeration for deletion."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ApiException, NotFoundException
from hindsight_client_api.models.list_memory_units_response import ListMemoryUnitsResponse
from hindsight_client_api.models.memory_unit_list_item import MemoryUnitListItem
from hindsight_client_api.models.operations_list_response import OperationsListResponse

from digital_souls_memory_hindsight.hindsight_deletion import HindsightDeletion

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("active", ["none", "pending", "processing"])
def test_busy_bank_counts_retain_and_consolidation_without_type_filter(active: str) -> None:
    client = MagicMock(spec=Hindsight)

    async def listing(bank_id: str, status: str, _request_timeout: float) -> OperationsListResponse:
        return OperationsListResponse(
            bank_id=bank_id, operations=[], total=int(status == active), limit=100, offset=0
        )

    client.operations.list_operations = AsyncMock(side_effect=listing)
    assert asyncio.run(HindsightDeletion(client).bank_busy("bank")) == (active != "none")
    assert client.operations.list_operations.await_args_list
    assert all(
        "type" not in call.kwargs for call in client.operations.list_operations.await_args_list
    )


def test_memory_ids_includes_later_pages_and_observations() -> None:
    client = MagicMock(spec=Hindsight)
    client.memory.list_memories = AsyncMock(
        side_effect=[
            ListMemoryUnitsResponse(
                items=[MemoryUnitListItem(id="fact", fact_type="world")], total=2, limit=1, offset=0
            ),
            ListMemoryUnitsResponse(
                items=[MemoryUnitListItem(id="observation", fact_type="observation")],
                total=2,
                limit=1,
                offset=1,
            ),
        ]
    )
    assert asyncio.run(HindsightDeletion(client).memory_ids("bank")) == frozenset(
        {"fact", "observation"}
    )
    assert [call.kwargs["offset"] for call in client.memory.list_memories.await_args_list] == [0, 1]


@pytest.mark.parametrize("missing", [True, False])
@pytest.mark.parametrize("method", ["bank_busy", "memory_ids"])
def test_only_missing_bank_is_treated_as_empty(method: str, missing: bool) -> None:
    client = MagicMock(spec=Hindsight)
    error = NotFoundException(status=404) if missing else ApiException(status=500)
    client.operations.list_operations = AsyncMock(side_effect=error)
    client.memory.list_memories = AsyncMock(side_effect=error)
    operation = getattr(HindsightDeletion(client), method)
    if missing:
        assert asyncio.run(operation("bank")) == (False if method == "bank_busy" else frozenset())
    else:
        with pytest.raises(ApiException):
            asyncio.run(operation("bank"))


def test_nonadvancing_memory_page_does_not_hide_incomplete_snapshot() -> None:
    client = MagicMock(spec=Hindsight)
    client.memory.list_memories = AsyncMock(
        return_value=ListMemoryUnitsResponse(items=[], total=1, limit=100, offset=0)
    )
    with pytest.raises(RuntimeError):
        asyncio.run(HindsightDeletion(client).memory_ids("bank"))
