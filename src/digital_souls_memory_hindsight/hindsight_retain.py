"""Translate the official SDK's retain and operation responses once."""

from hindsight_client import Hindsight
from hindsight_client_api.exceptions import NotFoundException

from .outbox import RetainOperation, RetainResult


class HindsightRetain:
    def __init__(self, client: Hindsight) -> None:
        self._client = client

    async def execute(self, operation: RetainOperation) -> RetainResult:
        await self._client.acreate_bank(bank_id=operation.bank_id)
        if operation.update_mode in {"replace", "delete"}:
            if await self._has_other_active_operations(operation):
                return RetainResult("pending")
        if operation.update_mode == "delete":
            try:
                deleted = await self._client.documents.delete_document(
                    bank_id=operation.bank_id,
                    document_id=operation.document_id,
                    _request_timeout=10.0,
                )
            except NotFoundException:
                return RetainResult("completed")
            return RetainResult("completed" if deleted.success else "failed")
        retained = await self._client.aretain(
            bank_id=operation.bank_id,
            document_id=operation.document_id,
            content=operation.content,
            timestamp=operation.occurred_at,
            update_mode=operation.update_mode,
            retain_async=True,
            operation_id=operation.operation_id,
        )
        if not retained.success or retained.operation_id != operation.operation_id:
            raise RuntimeError("Retain was not accepted with the saved operation identifier")
        result = await self._client.operations.get_operation_status(
            bank_id=operation.bank_id,
            operation_id=operation.operation_id,
            _request_timeout=10.0,
        )
        if result.status == "completed":
            return RetainResult("completed")
        if result.status in {"pending", "processing"}:
            return RetainResult("pending")
        if result.status in {"failed", "cancelled"}:
            return RetainResult("failed")
        raise RuntimeError("Retain operation status is unavailable")

    async def _has_other_active_operations(self, operation: RetainOperation) -> bool:
        for status in ("pending", "processing"):
            operations = await self._client.operations.list_operations(
                bank_id=operation.bank_id, status=status, _request_timeout=10.0
            )
            if any(value.operation_id != operation.operation_id for value in operations.operations):
                return True
        return False
