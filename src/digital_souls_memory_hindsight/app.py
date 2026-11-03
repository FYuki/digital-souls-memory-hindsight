"""HTTP entry point for the Phase 1 service."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from hindsight_client import Hindsight

from digital_souls_memory_hindsight.anchor_http import anchor_router
from digital_souls_memory_hindsight.anchors.service import AnchorService
from digital_souls_memory_hindsight.deletion.execution import DeletionExecutor
from digital_souls_memory_hindsight.deletion.service import DeletionService
from digital_souls_memory_hindsight.deletion_http import AuthorCredentials, deletion_router
from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from digital_souls_memory_hindsight.hindsight_deletion import HindsightDeletion
from digital_souls_memory_hindsight.hindsight_recall import HindsightRecall
from digital_souls_memory_hindsight.hindsight_retain import HindsightRetain
from digital_souls_memory_hindsight.identity.ports import LLMGateway
from digital_souls_memory_hindsight.identity.service import IdentityService
from digital_souls_memory_hindsight.identity_http import identity_router
from digital_souls_memory_hindsight.postgres_anchors import PostgresAnchorStore
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_identity import PostgresIdentityStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.postgres_provision import PostgresProvisionStore
from digital_souls_memory_hindsight.recall.service import RecallService
from digital_souls_memory_hindsight.recall_http import recall_router
from digital_souls_memory_hindsight.settings import load_settings
from digital_souls_memory_hindsight.worker import Worker


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = application.state.settings
    ledger = application.state.ledger_service
    client = (
        Hindsight(base_url=settings.hindsight_url, timeout=10.0) if settings.hindsight_url else None
    )
    application.state.anchor_service = AnchorService(
        PostgresAnchorStore(settings.database_url), ledger
    )
    deletion_store = PostgresDeletionStore(settings.database_url)
    application.state.deletion_service = DeletionService(deletion_store, ledger)
    application.state.recall_service = RecallService(
        ledger,
        HindsightRecall(client, min_reranker_score=settings.recall_min_reranker_score)
        if client is not None
        else None,
        PostgresProvisionStore(settings.database_url),
        application.state.anchor_service,
        application.state.deletion_service,
    )
    worker = Worker(
        PostgresOutbox(settings.database_url),
        HindsightRetain(client) if client is not None else None,
        ledger,
        deletion=DeletionExecutor(
            application.state.deletion_service,
            deletion_store,
            HindsightDeletion(client) if client is not None else None,
        ),
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


def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app(*, llm_gateway: LLMGateway | None = None) -> FastAPI:
    application = FastAPI(lifespan=lifespan)
    settings = load_settings()
    application.state.settings = settings
    application.state.ledger_service = EvidenceService(
        PostgresEvidenceStore(settings.database_url),
        settings.character_ids,
        delivery_timeout_seconds=settings.delivery_timeout_seconds,
    )
    application.state.identity_service = IdentityService(
        PostgresIdentityStore(settings.database_url), llm_gateway
    )
    application.include_router(evidence_router(lambda: application.state.ledger_service))
    application.include_router(recall_router(lambda: application.state.recall_service))
    application.include_router(anchor_router(lambda: application.state.anchor_service))
    application.include_router(identity_router(lambda: application.state.identity_service))
    application.include_router(
        deletion_router(
            lambda: application.state.deletion_service,
            AuthorCredentials(settings.author_id, settings.author_token)
            if settings.author_id is not None and settings.author_token is not None
            else None,
        )
    )
    application.get("/health")(health)
    return application


app = create_app()
ledger_service = app.state.ledger_service
