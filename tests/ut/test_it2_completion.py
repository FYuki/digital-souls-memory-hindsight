"""Completion snapshots must prove retain, consolidation and outbox success."""

import asyncio
from importlib import import_module
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import NotFoundException
from hindsight_client_api.models.bank_stats_response import BankStatsResponse
from hindsight_client_api.models.operation_response import OperationResponse
from hindsight_client_api.models.operation_status_response import OperationStatusResponse
from hindsight_client_api.models.operations_list_response import OperationsListResponse

pytestmark = pytest.mark.ut


@pytest.fixture
def completion() -> ModuleType:
    return import_module("tests.it2.completion")


def _snapshot() -> dict[str, object]:
    return {
        "outbox": [{"operation_id": "retain-1", "status": "completed", "update_mode": "append"}],
        "operations": [
            {"operation_id": "retain-1", "status": "completed", "type": "retain"},
            {"operation_id": "consolidate-1", "status": "completed", "type": "consolidation"},
        ],
        "pending_consolidation": 0,
        "failed_consolidation": 0,
    }


def test_completion_requires_all_three_sources_of_success(completion: ModuleType) -> None:
    assert completion.evaluate_completion(**_snapshot()).status == "complete"


@pytest.mark.parametrize(
    "change",
    [
        {"outbox": []},
        {"operations": []},
        {"operations": [{"operation_id": "retain-1", "status": "completed", "type": "retain"}]},
        {"pending_consolidation": 1},
        {"outbox": [{"operation_id": "retain-1", "status": "pending", "update_mode": "append"}]},
        {"outbox": [{"operation_id": "retain-1", "status": "processing", "update_mode": "append"}]},
        {
            "operations": [
                {"operation_id": "other-retain", "status": "completed", "type": "retain"},
                {"operation_id": "consolidate-1", "status": "completed", "type": "consolidation"},
            ]
        },
    ],
)
def test_completion_does_not_confuse_empty_or_partial_work_with_success(
    completion: ModuleType, change: dict[str, object]
) -> None:
    assert completion.evaluate_completion(**(_snapshot() | change)).status == "waiting"


@pytest.mark.parametrize("status", ["pending", "processing", "failed", "cancelled", "unknown"])
def test_every_operation_including_consolidation_affects_completion(
    completion: ModuleType, status: str
) -> None:
    operations = [
        {"operation_id": "retain-1", "status": "completed", "type": "retain"},
        {"operation_id": "consolidate-1", "status": "completed", "type": "consolidation"},
        {"operation_id": "consolidate-2", "status": status, "type": "consolidation"},
    ]
    expected = "waiting" if status in {"pending", "processing"} else "failed"
    assert (
        completion.evaluate_completion(**(_snapshot() | {"operations": operations})).status
        == expected
    )


@pytest.mark.parametrize("status", ["failed", "cancelled", "unknown"])
def test_outbox_failure_cannot_be_hidden_by_completed_hindsight_operations(
    completion: ModuleType, status: str
) -> None:
    snapshot = _snapshot() | {
        "outbox": [{"operation_id": "retain-1", "status": status, "update_mode": "append"}]
    }
    assert completion.evaluate_completion(**snapshot).status == "failed"


def test_failed_consolidation_stats_prevent_success(completion: ModuleType) -> None:
    snapshot = _snapshot() | {"failed_consolidation": 1}
    assert completion.evaluate_completion(**snapshot).status == "failed"


def test_failure_reason_uses_selected_operation_and_excludes_raw_errors(
    completion: ModuleType,
) -> None:
    operations = [
        {
            "operation_id": "z-last",
            "type": "retain",
            "status": "failed",
            "error_message": "synthetic-private-body",
        },
        {
            "operation_id": "a-first",
            "type": "consolidation",
            "status": "cancelled",
            "error_message": "synthetic-private-body",
        },
    ]
    result = completion.evaluate_completion(**(_snapshot() | {"operations": operations}))
    assert result.status == "failed"
    assert "a-first" in result.reason and "cancelled" in result.reason
    assert "z-last" not in result.reason
    assert "synthetic-private-body" not in result.reason


def test_delete_outbox_does_not_require_nonexistent_retain_operation(
    completion: ModuleType,
) -> None:
    snapshot = _snapshot() | {
        "outbox": [{"operation_id": "delete-1", "status": "completed", "update_mode": "delete"}]
    }
    assert completion.evaluate_completion(**snapshot).status == "complete"


def test_wait_observes_new_snapshots_until_complete_without_fixed_delay(
    completion: ModuleType,
) -> None:
    snapshots = iter([_snapshot() | {"pending_consolidation": 1}, _snapshot()])
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    result = completion.wait_for_completion(
        snapshot=lambda: next(snapshots), clock=lambda: elapsed[0], sleep=sleep
    )
    assert result.status == "complete"
    assert elapsed[0] < completion.COMPLETION_WAIT_LIMIT_SECONDS


def test_wait_times_out_with_injected_clock(completion: ModuleType) -> None:
    elapsed = [0.0]
    reads = [0]

    def snapshot() -> dict[str, object]:
        reads[0] += 1
        return _snapshot() | {"pending_consolidation": 1}

    def sleep(seconds: float) -> None:
        assert seconds > 0
        elapsed[0] += seconds

    result = completion.wait_for_completion(
        snapshot=snapshot, clock=lambda: elapsed[0], sleep=sleep
    )
    assert result.status == "timeout"
    assert elapsed[0] >= completion.COMPLETION_WAIT_LIMIT_SECONDS
    assert reads[0] > 1


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            {"pending_consolidation": 3, "pending_operations": 0},
            "completion wait limit exceeded: source memories await consolidation "
            "(outbox completed 1/1; retain completed 1/1; consolidation completed 1/1; "
            "pending_consolidation 3; pending_operations 0)",
        ),
        (
            {
                "outbox": [
                    {"operation_id": "retain-1", "status": "completed", "update_mode": "append"},
                    {"operation_id": "retain-2", "status": "pending", "update_mode": "append"},
                ]
            },
            "completion wait limit exceeded: outbox or operations are not complete "
            "(outbox completed 1/2; retain completed 1/1; consolidation completed 1/1; "
            "pending_consolidation 0; pending_operations 0)",
        ),
    ],
)
def test_timeout_reason_names_the_last_unmet_condition_with_fresh_counts(
    completion: ModuleType, change: dict[str, object], expected: str
) -> None:
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    result = completion.wait_for_completion(
        snapshot=lambda: _snapshot() | change, clock=lambda: elapsed[0], sleep=sleep
    )
    assert result == completion.CompletionResult("timeout", expected)


def test_wait_returns_failure_immediately_without_retry(completion: ModuleType) -> None:
    reads = [0]

    def snapshot() -> dict[str, object]:
        reads[0] += 1
        assert reads[0] == 1
        return _snapshot() | {"failed_consolidation": 1}

    def sleep(seconds: float) -> None:
        pytest.fail("failed operation must not be retried")

    result = completion.wait_for_completion(snapshot=snapshot, clock=lambda: 0.0, sleep=sleep)
    assert result.status == "failed"
    assert reads[0] == 1


def test_snapshot_reads_all_sdk_pages_and_only_target_bank_outbox(completion: ModuleType) -> None:
    client = MagicMock(spec=Hindsight)
    items = [
        OperationResponse(
            id=identifier,
            task_type=kind,
            items_count=1,
            created_at="2026-11-05T00:00:00Z",
            status="completed",
            error_message=None,
        )
        for identifier, kind in [
            ("retain-1", "batch_retain"),
            ("child-1", "retain"),
            ("consolidate-1", "consolidation"),
        ]
    ]
    client.operations.list_operations = AsyncMock(
        side_effect=[
            OperationsListResponse(
                bank_id="bank", operations=[item], total=3, limit=1, offset=index
            )
            for index, item in enumerate(items)
        ]
    )
    client.banks.get_agent_stats = AsyncMock(
        return_value=BankStatsResponse(
            bank_id="bank",
            total_nodes=1,
            total_links=0,
            total_documents=1,
            nodes_by_fact_type={},
            links_by_link_type={},
            links_by_fact_type={},
            links_breakdown={},
            pending_operations=0,
            failed_operations=0,
            pending_consolidation=0,
            failed_consolidation=0,
        )
    )
    with patch("tests.it2.completion.psycopg.connect") as connect:
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = [
            ("retain-1", "completed", "append")
        ]
        snapshot = asyncio.run(completion.read_completion_snapshot(client, "synthetic-db", "bank"))
    assert completion.evaluate_completion(**snapshot).status == "complete"
    calls = client.operations.list_operations.await_args_list
    assert [call.kwargs["offset"] for call in calls] == [0, 1, 2]
    assert all(call.kwargs["bank_id"] == "bank" and call.kwargs["limit"] == 100 for call in calls)
    assert connection.execute.call_args.args[1] == ("bank",)
    assert client.banks.get_agent_stats.await_args.kwargs["refresh"] is True


def _stats() -> BankStatsResponse:
    return BankStatsResponse(
        bank_id="bank",
        total_nodes=1,
        total_links=0,
        total_documents=1,
        nodes_by_fact_type={},
        links_by_link_type={},
        links_by_fact_type={},
        links_breakdown={},
        pending_operations=0,
        failed_operations=0,
        pending_consolidation=0,
        failed_consolidation=0,
    )


def _operation(identifier: str, kind: str) -> OperationResponse:
    return OperationResponse(
        id=identifier,
        task_type=kind,
        items_count=1,
        created_at="2026-11-05T00:00:00Z",
        status="completed",
        error_message=None,
    )


def test_snapshot_looks_up_outbox_operations_that_paging_skipped(completion: ModuleType) -> None:
    # Hindsight 1.1.0 orders the listing by created_at only. A batch parent and its child
    # share created_at, so offset pages can repeat the child and skip the parent (#89).
    client = MagicMock(spec=Hindsight)
    pages = [[_operation("child-1", "retain")], [_operation("child-1", "retain")]]
    pages.append([_operation("consolidate-1", "consolidation")])
    client.operations.list_operations = AsyncMock(
        side_effect=[
            OperationsListResponse(bank_id="bank", operations=page, total=3, limit=1, offset=index)
            for index, page in enumerate(pages)
        ]
    )
    client.operations.get_operation_status = AsyncMock(
        return_value=OperationStatusResponse(
            operation_id="retain-1", status="completed", operation_type="batch_retain"
        )
    )
    client.banks.get_agent_stats = AsyncMock(return_value=_stats())
    with patch("tests.it2.completion.psycopg.connect") as connect:
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = [
            ("retain-1", "completed", "append"),
            ("delete-1", "completed", "delete"),
        ]
        snapshot = asyncio.run(completion.read_completion_snapshot(client, "synthetic-db", "bank"))
    assert completion.evaluate_completion(**snapshot).status == "complete"
    assert [row["operation_id"] for row in snapshot["operations"]] == [
        "child-1",
        "consolidate-1",
        "retain-1",
    ]
    lookup = client.operations.get_operation_status.await_args
    assert client.operations.get_operation_status.await_count == 1
    assert lookup.kwargs["bank_id"] == "bank" and lookup.kwargs["operation_id"] == "retain-1"


def test_snapshot_does_not_look_up_operations_already_listed(completion: ModuleType) -> None:
    client = MagicMock(spec=Hindsight)
    client.operations.list_operations = AsyncMock(
        return_value=OperationsListResponse(
            bank_id="bank",
            operations=[_operation("retain-1", "batch_retain")],
            total=1,
            limit=100,
            offset=0,
        )
    )
    client.operations.get_operation_status = AsyncMock()
    client.banks.get_agent_stats = AsyncMock(return_value=_stats())
    with patch("tests.it2.completion.psycopg.connect") as connect:
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = [
            ("retain-1", "completed", "append")
        ]
        asyncio.run(completion.read_completion_snapshot(client, "synthetic-db", "bank"))
    client.operations.get_operation_status.assert_not_awaited()


def test_snapshot_rejects_nonadvancing_sdk_page(completion: ModuleType) -> None:
    client = MagicMock(spec=Hindsight)
    client.operations.list_operations = AsyncMock(
        return_value=OperationsListResponse(
            bank_id="bank",
            operations=[],
            total=1,
            limit=100,
            offset=0,
        )
    )
    with pytest.raises(RuntimeError):
        asyncio.run(completion.read_completion_snapshot(client, "synthetic-db", "bank"))


def test_wait_does_not_accept_success_arriving_after_deadline(completion: ModuleType) -> None:
    now = [0.0]

    def snapshot() -> dict[str, object]:
        now[0] = completion.COMPLETION_WAIT_LIMIT_SECONDS + 1.0
        return _snapshot()

    result = completion.wait_for_completion(
        snapshot=snapshot, clock=lambda: now[0], sleep=lambda _: None
    )
    assert result.status == "timeout"


@pytest.mark.parametrize("change", [{"pending_operations": 1}, {"failed_operations": 1}])
def test_fresh_operation_counts_cannot_be_hidden_by_older_operation_list(
    completion: ModuleType,
    change: dict[str, int],
) -> None:
    result = completion.evaluate_completion(**(_snapshot() | change))
    assert result.status == ("failed" if "failed_operations" in change else "waiting")


def test_snapshot_treats_bank_before_first_projection_as_waiting(
    completion: ModuleType,
) -> None:
    client = MagicMock(spec=Hindsight)
    client.operations = MagicMock()
    client.operations.list_operations = AsyncMock(side_effect=NotFoundException(status=404))
    client.banks = MagicMock()
    outbox = [{"operation_id": "op", "status": "pending", "update_mode": "append"}]
    with patch.object(completion, "_read_outbox", return_value=outbox):
        snapshot = asyncio.run(completion.read_completion_snapshot(client, "synthetic-db", "bank"))
    assert snapshot["outbox"] == outbox
    assert snapshot["operations"] == []
    assert completion.evaluate_completion(**snapshot).status == "waiting"


def test_missing_bank_after_completed_outbox_never_becomes_complete(
    completion: ModuleType,
) -> None:
    client = MagicMock(spec=Hindsight)
    client.operations = MagicMock()
    client.operations.list_operations = AsyncMock(side_effect=NotFoundException(status=404))
    client.banks = MagicMock()
    outbox = [{"operation_id": "op", "status": "completed", "update_mode": "append"}]
    with patch.object(completion, "_read_outbox", return_value=outbox):
        snapshot = asyncio.run(completion.read_completion_snapshot(client, "synthetic-db", "bank"))
    assert completion.evaluate_completion(**snapshot).status == "waiting"
