"""HTTP entry point for the Phase 1 service."""

from fastapi import FastAPI

from digital_souls_memory_hindsight.settings import load_settings

app = FastAPI()
app.state.settings = load_settings()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
