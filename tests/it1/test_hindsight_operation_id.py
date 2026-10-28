"""Measure append idempotency against the pinned, unmodified Hindsight server."""

import asyncio
import json
import os
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from hindsight_client import Hindsight

pytestmark = pytest.mark.it1
STAMP = "2026-10-27T00:00:00Z"


def operation_id(turn_id: str, bank_id: str, document_id: str) -> str:
    # This probe is independent of the future production identifier generator.
    return str(uuid5(NAMESPACE_URL, json.dumps([turn_id, bank_id, document_id])))


async def append_and_wait(
    client: Hindsight, bank_id: str, document_id: str, turn_id: str, content: str
) -> None:
    identifier = operation_id(turn_id, bank_id, document_id)
    retained = await client.aretain(
        bank_id=bank_id,
        document_id=document_id,
        content=content,
        timestamp=STAMP,
        update_mode="append",
        retain_async=True,
        operation_id=identifier,
    )
    assert retained.success, retained.to_dict()
    assert retained.operation_id == identifier, retained.to_dict()
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        status = await client.operations.get_operation_status(
            bank_id=bank_id, operation_id=identifier, _request_timeout=5.0
        )
        assert status.status not in {"failed", "cancelled"}, status.to_dict()
        if status.status == "completed":
            return
        assert asyncio.get_running_loop().time() < deadline, status.to_dict()
        await asyncio.sleep(0.25)


async def document_text(client: Hindsight, bank_id: str, document_id: str) -> str:
    document = await client.documents.get_document(
        bank_id=bank_id, document_id=document_id, _request_timeout=5.0
    )
    assert document.original_text is not None
    return document.original_text


def test_completed_operation_retry_does_not_append_again() -> None:
    async def exercise() -> None:
        client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
        token = uuid4().hex
        bank_id, document_id, turn_id = "it1-" + token, "session-" + token, "turn-" + token
        content = f"架空の花子は識別子{token}のお茶が好きです。"
        try:
            await client.acreate_bank(bank_id=bank_id)
            try:
                await append_and_wait(client, bank_id, document_id, turn_id, content)
                before = await document_text(client, bank_id, document_id)
                assert before.count(content) == 1
                await append_and_wait(client, bank_id, document_id, turn_id, content)
                after = await document_text(client, bank_id, document_id)
                assert after == before
                assert after.count(content) == 1
            finally:
                await client.banks.delete_bank(bank_id=bank_id, _request_timeout=10.0)
        finally:
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise())


def test_distinct_turn_with_identical_content_is_appended() -> None:
    async def exercise() -> None:
        client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
        token = uuid4().hex
        bank_id, document_id = "it1-" + token, "session-" + token
        content = f"架空の花子は識別子{token}のお茶が好きです。"
        try:
            await client.acreate_bank(bank_id=bank_id)
            try:
                await append_and_wait(client, bank_id, document_id, "first-" + token, content)
                assert (await document_text(client, bank_id, document_id)).count(content) == 1
                await append_and_wait(client, bank_id, document_id, "second-" + token, content)
                assert (await document_text(client, bank_id, document_id)).count(content) == 2
            finally:
                await client.banks.delete_bank(bank_id=bank_id, _request_timeout=10.0)
        finally:
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise())


def test_same_turn_can_be_appended_once_to_each_bank() -> None:
    async def exercise() -> None:
        client = Hindsight(base_url=os.environ["HINDSIGHT_URL"], timeout=15.0)
        token = uuid4().hex
        document_id, turn_id = "session-" + token, "turn-" + token
        content = f"架空の花子は識別子{token}のお茶が好きです。"
        created: list[str] = []
        try:
            try:
                for suffix in ("a", "b"):
                    bank_id = f"it1-{token}-{suffix}"
                    await client.acreate_bank(bank_id=bank_id)
                    created.append(bank_id)
                    await append_and_wait(client, bank_id, document_id, turn_id, content)
                    await append_and_wait(client, bank_id, document_id, turn_id, content)
                for bank_id in created:
                    assert (await document_text(client, bank_id, document_id)).count(content) == 1
            finally:
                for bank_id in created:
                    await client.banks.delete_bank(bank_id=bank_id, _request_timeout=10.0)
        finally:
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise())
