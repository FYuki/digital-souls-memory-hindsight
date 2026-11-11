"""Fixture-model execution of the supervisor scenario through real storage and worker."""

import asyncio
import importlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.character_state.domain import StateInput
from tests.consistency_doubles import Gateway, Reviewer
from tests.it2.e2e import STEPS, load_e2e_case
from tests.it2.e2e_run import PostgresE2EState, run_e2e_trial
from tests.it2.quality_run import HindsightQualityPort
from tests.it2.test_extraction_quality import _open_client

pytestmark = pytest.mark.it1


class StateGateway:
    async def generate_state(
        self,
        character_id: str,
        session_id: str,
        surface: str,
        turns: tuple[StateInput, ...],
    ) -> object:
        return {
            "mood": "穏やか",
            "relationship_temperature": "温かい",
            "current_topic": turns[-1].text[:100],
        }


def test_http_scenario_observes_changes_state_generations_and_completed_deletion(
    database: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    character = "e2e-" + uuid4().hex
    token = "synthetic-e2e-author-only"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", json.dumps([character]))
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", token)
    app = importlib.import_module("digital_souls_memory_hindsight.app")
    case = load_e2e_case(Path("tests/it2/cases/e2e.json"))
    with (
        TestClient(
            app.create_app(
                llm_gateway=Gateway(),
                reviewer=Reviewer(),
                context_gateway=StateGateway(),
            )
        ) as client,
        asyncio.Runner() as runner,
    ):
        hindsight = runner.run(_open_client(os.environ["HINDSIGHT_URL"]))
        try:
            result = run_e2e_trial(
                case=case,
                character_id=character,
                client=client,
                state=PostgresE2EState(database),
                inspection=HindsightQualityPort(hindsight, runner, database),
                author_token=token,
                clock=time.monotonic,
                sleep=time.sleep,
            )
        finally:
            runner.run(hindsight.aclose())  # type: ignore[no-untyped-call]
    assert result["cases"] == dict.fromkeys(STEPS, True), json.dumps(
        result["failures"], ensure_ascii=False
    )
    assert result["completion"] == "complete"
