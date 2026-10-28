"""Retain acceptance is distinct from completion; timestamps are event times."""

import asyncio
import importlib
from datetime import UTC, datetime
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from hindsight_client import Hindsight
from hindsight_client_api.api.documents_api import DocumentsApi
from hindsight_client_api.exceptions import ApiException, NotFoundException
from hindsight_client_api.models.delete_document_response import DeleteDocumentResponse
from hindsight_client_api.models.operation_status_response import OperationStatusResponse
from hindsight_client_api.models.operations_list_response import OperationsListResponse
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
    client.acreate_bank = AsyncMock()
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
    client.acreate_bank.assert_awaited_once_with(bank_id="bank")
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


@pytest.mark.parametrize("active", ["none", "own", "pending", "processing"])
@pytest.mark.parametrize("mode", ["replace", "delete"])
def test_rebuild_waits_for_other_work_but_not_its_own_operation(
    active: str, mode: Literal["replace", "delete"]
) -> None:
    from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain

    operation = RetainOperation(
        "turn",
        "bank",
        "session",
        "架空のお茶" if mode == "replace" else "",
        datetime(2020, 1, 1, tzinfo=UTC),
        mode,
        7,
    )
    client = MagicMock(spec=Hindsight)
    client.acreate_bank = AsyncMock()
    client.documents = MagicMock(spec=DocumentsApi)
    client.documents.delete_document = AsyncMock(
        return_value=DeleteDocumentResponse(
            success=True, message="Deleted", document_id="session", memory_units_deleted=2
        )
    )

    async def list_operations(
        bank_id: str, status: str, _request_timeout: float
    ) -> OperationsListResponse:
        included = status == active or (active == "own" and status == "pending")
        return OperationsListResponse.model_validate(
            {
                "bank_id": bank_id,
                "total": int(included),
                "limit": 100,
                "offset": 0,
                "operations": [
                    {
                        "id": "synthetic-task",
                        "operation_id": operation.operation_id if active == "own" else "other",
                        "task_type": "retain" if active == "own" else "consolidation",
                        "items_count": 1,
                        "created_at": "2026-10-28T00:00:00Z",
                        "status": status,
                        "error_message": None,
                    }
                ]
                if included
                else [],
            }
        )

    client.operations.list_operations = AsyncMock(side_effect=list_operations)
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
            operation_id=operation.operation_id, status="completed"
        )
    )
    result = asyncio.run(HindsightRetain(client).execute(operation))
    if active in {"pending", "processing"}:
        assert result.status == "pending"
        client.aretain.assert_not_awaited()
        client.documents.delete_document.assert_not_awaited()
    else:
        assert result.status == "completed"
        if mode == "delete":
            client.documents.delete_document.assert_awaited_once()
            sent = client.documents.delete_document.await_args.kwargs
            assert sent["bank_id"] == operation.bank_id
            assert sent["document_id"] == operation.document_id
            client.aretain.assert_not_awaited()
        else:
            sent = client.aretain.await_args.kwargs
            assert sent["update_mode"] == "replace"
            assert sent["operation_id"] == operation.operation_id
            assert sent["content"] == operation.content
            assert sent["timestamp"] == operation.occurred_at


def test_delete_retry_completes_when_document_is_already_missing() -> None:
    from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain

    operation = RetainOperation(
        "turn", "bank", "session", "", datetime(2020, 1, 1, tzinfo=UTC), "delete", 7
    )
    client = MagicMock(spec=Hindsight)
    client.acreate_bank = AsyncMock()
    client.operations.list_operations = AsyncMock(
        return_value=OperationsListResponse(
            bank_id="bank", operations=[], total=0, limit=100, offset=0
        )
    )
    client.documents = MagicMock(spec=DocumentsApi)
    client.documents.delete_document = AsyncMock(
        side_effect=[
            DeleteDocumentResponse(
                success=True, message="Deleted", document_id="session", memory_units_deleted=2
            ),
            NotFoundException(status=404, reason="Not Found"),
        ]
    )
    client.aretain = AsyncMock(
        side_effect=AssertionError("Delete must use the document deletion API")
    )

    async def exercise() -> None:
        executor = HindsightRetain(client)
        assert (await executor.execute(operation)).status == "completed"
        assert (await executor.execute(operation)).status == "completed"

    asyncio.run(exercise())
    assert client.documents.delete_document.await_count == 2
    client.aretain.assert_not_awaited()


@pytest.mark.parametrize("status", [422, 500])
def test_delete_error_is_not_marked_completed(status: int) -> None:
    from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain

    operation = RetainOperation(
        "turn", "bank", "session", "", datetime(2020, 1, 1, tzinfo=UTC), "delete", 7
    )
    client = MagicMock(spec=Hindsight)
    client.acreate_bank = AsyncMock()
    client.operations.list_operations = AsyncMock(
        return_value=OperationsListResponse(
            bank_id="bank", operations=[], total=0, limit=100, offset=0
        )
    )
    client.documents = MagicMock(spec=DocumentsApi)
    client.documents.delete_document = AsyncMock(side_effect=ApiException(status=status))
    client.aretain = AsyncMock(
        side_effect=AssertionError("Delete must use the document deletion API")
    )
    with pytest.raises(ApiException):
        asyncio.run(HindsightRetain(client).execute(operation))
    client.aretain.assert_not_awaited()


def test_unsuccessful_delete_response_is_failed() -> None:
    from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain

    operation = RetainOperation(
        "turn", "bank", "session", "", datetime(2020, 1, 1, tzinfo=UTC), "delete", 7
    )
    client = MagicMock(spec=Hindsight)
    client.acreate_bank = AsyncMock()
    client.operations.list_operations = AsyncMock(
        return_value=OperationsListResponse(
            bank_id="bank", operations=[], total=0, limit=100, offset=0
        )
    )
    client.documents = MagicMock(spec=DocumentsApi)
    client.documents.delete_document = AsyncMock(
        return_value=DeleteDocumentResponse(
            success=False, message="Rejected", document_id="session", memory_units_deleted=0
        )
    )
    client.aretain = AsyncMock()
    assert asyncio.run(HindsightRetain(client).execute(operation)).status == "failed"
    client.aretain.assert_not_awaited()
