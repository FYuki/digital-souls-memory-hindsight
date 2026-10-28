"""Retain acceptance is distinct from completion; timestamps are event times."""

import asyncio
import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from hindsight_client import Hindsight
from hindsight_client_api.models.operation_status_response import OperationStatusResponse
from hindsight_client_api.models.retain_response import RetainResponse

from digital_souls_memory_hindsight.outbox import RetainOperation

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("pending", "pending"),
        ("processing", "pending"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "failed"),
    ],
)
def test_retain_passes_saved_identity_payload_and_time_and_checks_completion(
    status: str,
    expected: str,
) -> None:
    adapter = importlib.import_module("digital_souls_memory_hindsight.hindsight_retain")
    operation = RetainOperation(
        "turn", "bank", "session", "架空のお茶", datetime(2020, 1, 1, tzinfo=UTC)
    )
    client = MagicMock(spec=Hindsight)
    client.aretain = AsyncMock(
        return_value=RetainResponse.model_validate(
            {
                "success": True,
                "bank_id": "bank",
                "items_count": 1,
                "async": True,
                "operation_id": operation.operation_id,
            }
        )
    )
    client.operations.get_operation_status = AsyncMock(
        return_value=OperationStatusResponse(
            operation_id=operation.operation_id,
            status=status,
        )
    )
    result = asyncio.run(adapter.HindsightRetain(client).execute(operation))
    assert result.status == expected
    client.aretain.assert_awaited_once_with(
        bank_id="bank",
        document_id="session",
        content=operation.content,
        timestamp=operation.occurred_at,
        update_mode="append",
        retain_async=True,
        operation_id=operation.operation_id,
    )
    client.operations.get_operation_status.assert_awaited_once_with(
        bank_id="bank",
        operation_id=operation.operation_id,
        _request_timeout=10.0,
    )


def test_transport_failure_does_not_become_success() -> None:
    adapter = importlib.import_module("digital_souls_memory_hindsight.hindsight_retain")
    client = MagicMock(spec=Hindsight)
    client.aretain = AsyncMock(side_effect=TimeoutError("synthetic private detail"))
    operation = RetainOperation(
        "turn", "bank", "session", "架空のお茶", datetime(2020, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(TimeoutError):
        asyncio.run(adapter.HindsightRetain(client).execute(operation))


@pytest.mark.parametrize("success", [False, True])
def test_rejected_or_mismatched_operation_is_not_marked_complete(success: bool) -> None:
    adapter = importlib.import_module("digital_souls_memory_hindsight.hindsight_retain")
    operation = RetainOperation(
        "turn", "bank", "session", "架空のお茶", datetime(2020, 1, 1, tzinfo=UTC)
    )
    client = MagicMock(spec=Hindsight)
    client.aretain = AsyncMock(
        return_value=RetainResponse.model_validate(
            {
                "success": success,
                "bank_id": "bank",
                "items_count": 1,
                "async": True,
                "operation_id": "unexpected" if success else operation.operation_id,
            }
        )
    )
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.HindsightRetain(client).execute(operation))
