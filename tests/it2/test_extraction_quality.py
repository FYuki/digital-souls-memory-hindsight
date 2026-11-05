"""Supervisor-only real-model entry; retain every trial before asserting acceptance."""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from hindsight_client import Hindsight

from .cases_contract import load_cases
from .quality_contract import GENERATION_MODELS
from .quality_run import (
    HindsightQualityPort,
    HttpIngestPort,
    JsonlTrialRecorder,
    run_quality_trials,
)
from .report import validate_safe
from .scoring import SCORER_VERSION

pytestmark = pytest.mark.it2


async def _open_client(url: str) -> Hindsight:
    return Hindsight(base_url=url, timeout=15.0, max_attempts=1)


def test_extraction_quality() -> None:
    model = os.environ["IT2_GENERATION_MODEL"]
    if model not in GENERATION_MODELS:
        raise ValueError("Unknown generation model")
    target = Path(os.environ["IT2_RESULT_DIR"]) / f"extraction-quality-{model}.json"
    if target.exists():
        raise FileExistsError("Quality result already exists")
    recorder = JsonlTrialRecorder(target.with_suffix(".progress.jsonl"))
    cases = load_cases(Path(__file__).with_name("cases"))
    with (
        asyncio.Runner() as runner,
        httpx.Client(
            base_url=os.environ["SERVICE_URL"],
            timeout=120.0,
            trust_env=False,
        ) as http,
    ):
        client = runner.run(_open_client(os.environ["HINDSIGHT_URL"]))
        try:
            inspection = HindsightQualityPort(client, runner, os.environ["DATABASE_URL"])
            result = run_quality_trials(
                cases=cases,
                ingest=HttpIngestPort(http),
                completion=inspection,
                reader=inspection,
                clock=time.monotonic,
                recorder=recorder,
            )
        finally:
            runner.run(client.aclose())  # type: ignore[no-untyped-call]
    payload = {"model": model, "scorer_version": SCORER_VERSION, **result}
    validate_safe(payload)
    with target.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write("\n")
    assert result["verdict"] == "PASS", "Required extraction quality cases failed; results saved"
