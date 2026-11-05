"""Supervisor entries for recorded-score selection and service confirmation."""

import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import cast

import httpx
import pytest

from .abstention_run import HindsightCandidateReader, HttpAbstentionPort, run_abstention_trials
from .cases_contract import load_cases, validate_cases
from .quality_contract import GENERATION_MODELS
from .quality_run import HindsightQualityPort, JsonlTrialRecorder
from .report import validate_safe
from .scoring import SCORER_VERSION
from .test_extraction_quality import _open_client
from .threshold import THRESHOLDS, AbstentionTrial, evaluate_candidates, select_threshold

pytestmark = pytest.mark.it2


def _run(mode: str) -> None:
    model = os.environ["IT2_GENERATION_MODEL"]
    if model not in GENERATION_MODELS:
        raise ValueError("Unknown generation model")
    threshold = float(os.environ.get("RECALL_MIN_RERANKER_SCORE", "0.5"))
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid recall threshold")
    target = Path(os.environ["IT2_RESULT_DIR"]) / f"recall-threshold-{mode}-{model}.json"
    if target.exists():
        raise FileExistsError("Recall result already exists")
    if mode == "confirmation":
        selection = json.loads(
            target.with_name(f"recall-threshold-selection-{model}.json").read_text()
        )
        validate_safe(selection)
        if (
            selection.get("model") != model
            or selection.get("verdict") != "PASS"
            or selection.get("threshold") != threshold
        ):
            raise ValueError("Selection and service threshold differ")
    recorder = JsonlTrialRecorder(target.with_suffix(".progress.jsonl"))
    cases = load_cases(Path(__file__).with_name("cases"))
    validate_cases(cases)
    with (
        asyncio.Runner() as runner,
        httpx.Client(base_url=os.environ["SERVICE_URL"], timeout=120.0, trust_env=False) as http,
    ):
        client = runner.run(_open_client(os.environ["HINDSIGHT_URL"]))
        try:
            result = run_abstention_trials(
                cases=cases,
                ingest=HttpAbstentionPort(http),
                completion=HindsightQualityPort(client, runner, os.environ["DATABASE_URL"]),
                reader=HindsightCandidateReader(client, runner),
                clock=time.monotonic,
                recorder=recorder,
            )
        finally:
            runner.run(client.aclose())  # type: ignore[no-untyped-call]
    payload = {"model": model, "scorer_version": SCORER_VERSION, "threshold": threshold, **result}
    if mode == "selection":
        table = evaluate_candidates(
            cases=[c for c in cases if c["acceptance_id"] == "AC-R4"],
            trials=cast(list[AbstentionTrial], result["trials"]),
            thresholds=THRESHOLDS,
        )
        payload.update(evaluations=table, **select_threshold(table))
    validate_safe(payload)
    with target.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write("\n")
    assert payload["verdict"] == "PASS", "Recall threshold evaluation failed; results saved"


def test_select_threshold() -> None:
    _run("selection")


def test_confirm_threshold() -> None:
    _run("confirmation")
