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


def test_observation_sources_enumerates_all_pages_without_including_extracted_facts() -> None:
    client = MagicMock(spec=Hindsight)
    client.memory.list_memories = AsyncMock(
        side_effect=[
            ListMemoryUnitsResponse(
                items=[
                    MemoryUnitListItem(
                        id="first", fact_type="observation", source_memory_ids=["deleted", "kept"]
                    )
                ],
                total=2,
                limit=1,
                offset=0,
            ),
            ListMemoryUnitsResponse(
                items=[
                    MemoryUnitListItem(
                        id="second", fact_type="observation", source_memory_ids=["kept"]
                    )
                ],
                total=2,
                limit=1,
                offset=1,
            ),
        ]
    )
    operation = HindsightDeletion(client).observation_sources
    assert asyncio.run(operation("bank")) == {
        "first": frozenset({"deleted", "kept"}),
        "second": frozenset({"kept"}),
    }
    calls = client.memory.list_memories.await_args_list
    assert [call.kwargs["offset"] for call in calls] == [0, 1]
    assert all(call.kwargs["type"] == "observation" for call in calls)


@pytest.mark.parametrize("missing", [False, True])
def test_observation_source_enumeration_only_treats_missing_bank_as_empty(missing: bool) -> None:
    client = MagicMock(spec=Hindsight)
    client.memory.list_memories = AsyncMock(
        side_effect=NotFoundException(status=404) if missing else ApiException(status=500)
    )
    operation = HindsightDeletion(client).observation_sources
    if missing:
        assert asyncio.run(operation("bank")) == {}
    else:
        with pytest.raises(ApiException):
            asyncio.run(operation("bank"))


def test_observation_rebuild_uses_public_sdk_and_propagates_failure() -> None:
    client = MagicMock(spec=Hindsight)
    client.memory.clear_memory_observations = AsyncMock()
    operation = HindsightDeletion(client).clear_memory_observations
    asyncio.run(operation("bank", "kept-fact"))
    arguments = client.memory.clear_memory_observations.await_args
    assert arguments.kwargs["bank_id"] == "bank"
    assert arguments.kwargs["memory_id"] == "kept-fact"
    client.memory.clear_memory_observations.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        asyncio.run(operation("bank", "kept-fact"))


def test_bank_observation_clear_uses_public_sdk_and_propagates_failure() -> None:
    client = MagicMock(spec=Hindsight)
    client.banks.clear_observations = AsyncMock()
    adapter = HindsightDeletion(client)
    asyncio.run(adapter.clear_observations("bank"))
    assert client.banks.clear_observations.await_args.kwargs["bank_id"] == "bank"
    client.banks.clear_observations.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        asyncio.run(adapter.clear_observations("bank"))


def test_nonadvancing_observation_page_does_not_hide_residue() -> None:
    client = MagicMock(spec=Hindsight)
    client.memory.list_memories = AsyncMock(
        return_value=ListMemoryUnitsResponse(items=[], total=1, limit=100, offset=0)
    )
    with pytest.raises(RuntimeError):
        asyncio.run(HindsightDeletion(client).observation_sources("bank"))
