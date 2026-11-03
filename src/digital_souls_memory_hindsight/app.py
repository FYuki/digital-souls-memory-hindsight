"""HTTP entry point for the Phase 1 service."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from hindsight_client import Hindsight

from digital_souls_memory_hindsight.anchor_http import anchor_router
from digital_souls_memory_hindsight.anchors.service import AnchorService
from digital_souls_memory_hindsight.character_profile.service import CharacterProfileService
from digital_souls_memory_hindsight.character_profile_http import character_profile_router
from digital_souls_memory_hindsight.character_state.generator import CharacterStateGenerator
from digital_souls_memory_hindsight.character_state.ports import ContextGateway
from digital_souls_memory_hindsight.character_state.service import CharacterStateService
from digital_souls_memory_hindsight.character_state_http import character_state_router
from digital_souls_memory_hindsight.consistency.ports import Reviewer
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
from digital_souls_memory_hindsight.postgres_character_profile import PostgresCharacterProfileStore
from digital_souls_memory_hindsight.postgres_character_state import PostgresCharacterStateStore
from digital_souls_memory_hindsight.postgres_deletion import PostgresDeletionStore
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.postgres_identity import PostgresIdentityStore
from digital_souls_memory_hindsight.postgres_outbox import PostgresOutbox
from digital_souls_memory_hindsight.postgres_provision import PostgresProvisionStore
from digital_souls_memory_hindsight.postgres_resident import PostgresResidentStore
from digital_souls_memory_hindsight.postgres_voice import PostgresVoiceStore
from digital_souls_memory_hindsight.recall.service import RecallService
from digital_souls_memory_hindsight.recall_http import recall_router
from digital_souls_memory_hindsight.resident.service import ResidentService
from digital_souls_memory_hindsight.resident_http import resident_router
from digital_souls_memory_hindsight.settings import load_settings
from digital_souls_memory_hindsight.voice.service import VoiceService
from digital_souls_memory_hindsight.voice_http import voice_router
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
    application.state.resident_service = ResidentService(
        PostgresResidentStore(settings.database_url), ledger
    )
    application.state.voice_service = VoiceService(
        PostgresVoiceStore(settings.database_url),
        ledger,
        deletion_store,
        PostgresIdentityStore(settings.database_url),
        application.state.llm_gateway,
        application.state.reviewer,
    )
    application.state.character_profile_service = CharacterProfileService(
        PostgresCharacterProfileStore(settings.database_url), ledger, deletion_store
    )
    application.state.deletion_service = DeletionService(deletion_store, ledger)
    state_store = PostgresCharacterStateStore(settings.database_url)
    application.state.character_state_service = CharacterStateService(
        state_store, ledger, deletion_store, ttl_seconds=settings.character_state_ttl_seconds
    )
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
        state=CharacterStateGenerator(
            state_store,
            ledger,
            deletion_store,
            application.state.context_gateway,
            input_max_chars=settings.character_state_input_max_chars,
        )
        if application.state.context_gateway is not None
        else None,
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


def create_app(
    *,
    llm_gateway: LLMGateway | None = None,
    reviewer: Reviewer | None = None,
    context_gateway: ContextGateway | None = None,
) -> FastAPI:
    application = FastAPI(lifespan=lifespan)
    settings = load_settings()
    application.state.settings = settings
    application.state.context_gateway = context_gateway
    application.state.llm_gateway = llm_gateway
    application.state.reviewer = reviewer
    application.state.ledger_service = EvidenceService(
        PostgresEvidenceStore(settings.database_url),
        settings.character_ids,
        delivery_timeout_seconds=settings.delivery_timeout_seconds,
    )
    application.state.identity_service = IdentityService(
        PostgresIdentityStore(settings.database_url), llm_gateway, reviewer
    )
    application.include_router(evidence_router(lambda: application.state.ledger_service))
    application.include_router(recall_router(lambda: application.state.recall_service))
    application.include_router(resident_router(lambda: application.state.resident_service))
    application.include_router(anchor_router(lambda: application.state.anchor_service))
    application.include_router(identity_router(lambda: application.state.identity_service))
    application.include_router(voice_router(lambda: application.state.voice_service))
    application.include_router(
        character_profile_router(lambda: application.state.character_profile_service)
    )
    application.include_router(
        character_state_router(lambda: application.state.character_state_service)
    )
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
