"""HTTP entry point for the Phase 1 service."""

from fastapi import FastAPI

from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.evidence_http import evidence_router
from digital_souls_memory_hindsight.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory_hindsight.settings import load_settings

app = FastAPI()
app.state.settings = load_settings()
ledger_service = EvidenceService(
    PostgresEvidenceStore(app.state.settings.database_url),
    app.state.settings.character_ids,
    delivery_timeout_seconds=app.state.settings.delivery_timeout_seconds,
)


app.include_router(evidence_router(lambda: ledger_service))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
