"""Supervisor-only real-model consistency entry; imports require no configuration."""

import importlib
import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.consistency.ports import Reviewer
from digital_souls_memory_hindsight.identity.ports import LLMGateway
from digital_souls_memory_hindsight.openai_compatible import OpenAICompatibleGateway
from digital_souls_memory_hindsight.settings import load_settings

from .consistency import consistency_character_id, load_consistency_cases
from .consistency_run import (
    HttpConsistencyExecutor,
    PostgresConsistencyState,
    run_consistency_trials,
)
from .quality_run import JsonlTrialRecorder
from .report import validate_safe
from .scoring import SCORER_VERSION

pytestmark = pytest.mark.it2


class ApplicationClients:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def open(self, gateway: LLMGateway, reviewer: Reviewer) -> TestClient:
        module = importlib.import_module("digital_souls_memory_hindsight.app")
        self._monkeypatch.setattr(
            module, "Worker", MagicMock(return_value=MagicMock(run=AsyncMock()))
        )
        return TestClient(module.create_app(llm_gateway=gateway, reviewer=reviewer))


def test_consistency(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    if settings.llm is None:
        raise ValueError("Consistency IT2 requires MEMORY_LLM configuration")
    model = settings.llm.model
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Invalid consistency model filename")
    target = Path(os.environ["IT2_RESULT_DIR"]) / f"consistency-{model}.json"
    if target.exists():
        raise FileExistsError("Consistency result already exists")
    cases = load_consistency_cases(Path(__file__).with_name("cases") / "consistency.json")
    recorder = JsonlTrialRecorder(target.with_suffix(".progress.jsonl"))
    characters = [
        consistency_character_id(case_id=case["id"], trial=trial)
        for trial in (1, 2, 3)
        for case in cases
    ]
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", json.dumps(characters))
    author_token = "synthetic-consistency-author-only"
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", author_token)
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    gateway = OpenAICompatibleGateway(
        settings.llm.base_url, model, settings.llm.api_key, settings.llm.timeout_seconds
    )
    result = run_consistency_trials(
        cases=cases,
        executor=HttpConsistencyExecutor(
            gateway,
            gateway,
            ApplicationClients(monkeypatch),
            PostgresConsistencyState(settings.database_url, author_token),
        ),
        recorder=recorder,
    )
    payload = {"model": model, "scorer_version": SCORER_VERSION, **result}
    validate_safe(payload)
    with target.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write("\n")
    assert result["verdict"] == "PASS", "Required consistency cases failed; results saved"
