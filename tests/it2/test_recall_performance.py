"""Supervisor-only HTTP recall performance acceptance."""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from .performance_run import PostgresStateWaiter, run_performance
from .quality_contract import GENERATION_MODELS
from .quality_run import HindsightQualityPort, JsonlTrialRecorder
from .report import validate_safe
from .scoring import SCORER_VERSION
from .test_extraction_quality import _open_client

pytestmark = pytest.mark.it2


def test_recall_performance() -> None:
    model = os.environ["IT2_GENERATION_MODEL"]
    if model not in GENERATION_MODELS:
        raise ValueError("Unknown generation model")
    target = Path(os.environ["IT2_RESULT_DIR"]) / f"recall-performance-{model}.json"
    if target.exists():
        raise FileExistsError("Performance result already exists")
    recorder = JsonlTrialRecorder(target.with_suffix(".progress.jsonl"))
    with (
        asyncio.Runner() as runner,
        httpx.Client(base_url=os.environ["SERVICE_URL"], timeout=120.0, trust_env=False) as http,
    ):
        client = runner.run(_open_client(os.environ["HINDSIGHT_URL"]))
        try:
            inspection = HindsightQualityPort(client, runner, os.environ["DATABASE_URL"])
            result = run_performance(
                client=http,
                completion=inspection,
                reader=inspection,
                state=PostgresStateWaiter(os.environ["DATABASE_URL"]),
                sdk=client,
                runner=runner,
                model=model,
                recorder=recorder,
                clock=time.monotonic,
            )
        finally:
            runner.run(client.aclose())  # type: ignore[no-untyped-call]
    payload = {
        "model": model,
        "scorer_version": SCORER_VERSION,
        "threshold": float(os.environ.get("RECALL_MIN_RERANKER_SCORE", "0.5")),
        **result,
    }
    validate_safe(payload)
    with target.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write("\n")
    assert result["verdict"] == "PASS", "Recall performance failed; results saved"
