import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.it1


def test_japanese_retain_recall_and_automatic_consolidation_use_recorded_stub_requests() -> None:
    from hindsight_client import Hindsight

    base_url = os.environ["HINDSIGHT_URL"]
    request_log = Path(os.environ["STUB_REQUEST_LOG"])
    offset = request_log.stat().st_size
    token = uuid4().hex
    bank_id = "it1-" + token
    document_id = "synthetic-" + token
    content = f"架空の花子は識別子{token}のお茶が好きです。"

    async def exercise_hindsight() -> None:
        client = Hindsight(base_url=base_url, timeout=15.0)
        try:
            await client.acreate_bank(bank_id=bank_id)
            try:
                retained = await client.aretain(
                    bank_id=bank_id,
                    content=content,
                    document_id=document_id,
                    timestamp="2026-10-27T00:00:00Z",
                )
                assert retained.success
                recalled = await client.arecall(
                    bank_id=bank_id, query="お茶", types=["world", "experience"]
                )
                # A vector-only match would not demonstrate Japanese substring text search.
                assert any(
                    item.document_id == document_id
                    and content in item.text
                    and item.scores is not None
                    and item.scores.keyword is not None
                    for item in recalled.results
                )
                deadline = asyncio.get_running_loop().time() + 60
                while True:
                    operations = await client.operations.list_operations(
                        bank_id=bank_id, type="consolidation", _request_timeout=5.0
                    )
                    assert all(
                        item.status not in {"failed", "cancelled"} for item in operations.operations
                    ), operations.to_dict()
                    if operations.operations and all(
                        item.status == "completed" for item in operations.operations
                    ):
                        break
                    assert asyncio.get_running_loop().time() < deadline, operations.to_dict()
                    await asyncio.sleep(0.25)
            finally:
                await client.banks.delete_bank(bank_id=bank_id, _request_timeout=10.0)
        finally:
            # The pinned official SDK omits the return annotation on aclose.
            await client.aclose()  # type: ignore[no-untyped-call]

    asyncio.run(exercise_hindsight())
    with request_log.open(encoding="utf-8") as stream:
        stream.seek(offset)
        records = [json.loads(line) for line in stream if line.strip()]
    chats = [record["req"] for record in records if record["path"] == "/v1/chat/completions"]
    embeddings = [record["req"] for record in records if record["path"] == "/v1/embeddings"]
    assert any(
        content in message["content"] and "Content:" in message["content"]
        for request in chats
        for message in request["messages"]
        if isinstance(message["content"], str)
    )
    assert any(
        content in json.dumps(request["input"], ensure_ascii=False) for request in embeddings
    )
    assert any(
        message["content"].startswith("You are a memory consolidation system")
        for request in chats
        for message in request["messages"]
        if isinstance(message["content"], str)
    )
