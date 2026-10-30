"""HTTP entry point for the Phase 1 service."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from hindsight_client import Hindsight

from digital_souls_memory_hindsight.anchor_http import anchor_router
from digital_souls_memory_hindsight.anchors.service import AnchorService
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from digital_souls_memory_hindsight.hindsight_recall import HindsightRecall
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.postgres_anchors import PostgresAnchorStore
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.postgres_provision import PostgresProvisionStore
from digital_souls_memory_hindsight.recall.service import RecallService
from digital_souls_memory_hindsight.recall_http import recall_router
from digital_souls_memory_hindsight.settings import load_settings
from digital_souls_memory_hindsight.worker import Worker


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = application.state.settings
    client = (
        Hindsight(base_url=settings.hindsight_url, timeout=10.0) if settings.hindsight_url else None
    )
    application.state.anchor_service = AnchorService(
        PostgresAnchorStore(settings.database_url), ledger_service
    )
    application.state.recall_service = RecallService(
        ledger_service,
        HindsightRecall(client, min_reranker_score=settings.recall_min_reranker_score)
        if client is not None
        else None,
        PostgresProvisionStore(settings.database_url),
        application.state.anchor_service,
    )
    worker = Worker(
        PostgresOutbox(settings.database_url),
        HindsightRetain(client) if client is not None else None,
        ledger_service,
    )
    task = asyncio.create_task(worker.run())
    try:
        yield
    finally:
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            if client is not None:
                await client.aclose()  # type: ignore[no-untyped-call]


app = FastAPI(lifespan=lifespan)
app.state.settings = load_settings()
ledger_service = EvidenceService(
    PostgresEvidenceStore(app.state.settings.database_url),
    app.state.settings.character_ids,
    delivery_timeout_seconds=app.state.settings.delivery_timeout_seconds,
)


app.include_router(evidence_router(lambda: ledger_service))
app.include_router(recall_router(lambda: app.state.recall_service))
app.include_router(anchor_router(lambda: app.state.anchor_service))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
