"""想定する Core の呼び出し順で、Core の検証ではない。監督だけが実モデルで実行する。"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from digital_souls_memory_hindsight.settings import load_settings

from .e2e import load_e2e_case
from .e2e_run import PostgresE2EState, run_e2e_trials
from .quality_run import HindsightQualityPort, JsonlTrialRecorder
from .report import validate_safe
from .scoring import SCORER_VERSION
from .test_extraction_quality import _open_client

pytestmark = pytest.mark.it2


def test_e2e() -> None:
    model = os.environ["IT2_GENERATION_MODEL"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Invalid E2E model filename")
    target = Path(os.environ["IT2_RESULT_DIR"]) / f"e2e-{model}.json"
    progress = target.with_suffix(".progress.jsonl")
    if target.exists() or progress.exists():
        raise FileExistsError("E2E result or progress already exists")
    settings = load_settings()
    if settings.llm is None:
        raise ValueError("E2E requires component model configuration")
    author_token = os.environ["MEMORY_AUTHOR_TOKEN"]
    if not author_token.strip():
        raise ValueError("E2E requires synthetic author credentials")
    case = load_e2e_case(Path(__file__).with_name("cases") / "e2e.json")
    recorder = JsonlTrialRecorder(progress)
    with (
        asyncio.Runner() as runner,
        httpx.Client(
            base_url=os.environ["SERVICE_URL"],
            timeout=2 * settings.llm.timeout_seconds + 30,
            trust_env=False,
        ) as http,
    ):
        client = runner.run(_open_client(os.environ["HINDSIGHT_URL"]))
        try:
            result = run_e2e_trials(
                case=case,
                client=http,
                state=PostgresE2EState(settings.database_url),
                inspection=HindsightQualityPort(client, runner, settings.database_url),
                author_token=author_token,
                clock=time.monotonic,
                sleep=time.sleep,
                recorder=recorder,
            )
        finally:
            runner.run(client.aclose())  # type: ignore[no-untyped-call]
    payload = {
        "generation_model": model,
        "memory_llm_model": settings.llm.model,
        "scorer_version": SCORER_VERSION,
        "recall_min_reranker_score": settings.recall_min_reranker_score,
        **result,
    }
    validate_safe(payload)
    with target.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write("\n")
    assert result["verdict"] == "PASS", "Required E2E steps failed; all trial results saved"
