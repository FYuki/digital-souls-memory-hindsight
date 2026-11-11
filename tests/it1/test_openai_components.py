"""Environment-created app uses HTTP stub, real storage and the running worker."""

import importlib
import json
import os
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.character_state.domain import validate_output
from tests.ut.test_evidence_http import session_body, turn_body
from tests.ut.test_identity_http import seed_body
from tests.ut.test_voice_http import voice_body

pytestmark = pytest.mark.it1


def test_environment_app_registers_reviewed_identity_voice_and_generates_state(
    database: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request_log = Path(os.environ["STUB_REQUEST_LOG"])
    request_offset = len(request_log.read_text().splitlines())
    character = "oai-" + uuid4().hex
    root = "/characters/" + character
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", json.dumps([character]))
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://stub:8080/v1")
    monkeypatch.setenv("MEMORY_LLM_MODEL", "stub")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "stub")
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT_SECONDS", "10")
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    with TestClient(module.create_app()) as client:
        initial = client.post(root + "/temperaments", json={"description": "架空の穏やかな気質"})
        assert initial.status_code == 201, initial.text
        assert initial.json()["review"] is None
        seeded = client.post(root + "/personality-profiles/author-seed", json=seed_body())
        assert seeded.status_code == 201, seeded.text
        updated = client.post(root + "/temperaments", json={"description": "架空の温和な気質"})
        assert updated.status_code == 201, updated.text
        for response in (seeded, updated):
            review = response.json()["review"]
            assert set(review) == {"consistent", "reason"}
            assert review["consistent"] is True
            assert isinstance(review["reason"], str) and review["reason"].strip()
        assert client.get(root + "/personality-profiles/1").json() == {
            k: v for k, v in seeded.json().items() if k != "review"
        }
        assert client.get(root + "/temperaments/2").json() == {
            k: v for k, v in updated.json().items() if k != "review"
        }
        voice = client.post(root + "/voice-exemplars", json=voice_body())
        assert voice.status_code == 201, voice.text
        assert voice.json()["consistency_review"]["consistent"] is True
        later = client.post(root + "/voice-exemplars", json=voice_body(exemplar_id="second"))
        assert later.status_code == 201, later.text
        assert later.json()["consistency_review"] is None
        assert client.post("/sessions", json=session_body()).status_code == 201
        turn = client.post("/turns", json=turn_body(actor=character, perceived_by=[character]))
        assert turn.status_code == 201, turn.text
        deadline = time.monotonic() + 30
        while True:
            response = client.get(root + "/sessions/s-1/state", params={"surface": "Web"})
            assert response.status_code == 200, response.text
            state = response.json()["state"]
            if state is not None:
                assert validate_output(state)
                break
            assert time.monotonic() < deadline, "Worker did not publish a valid state"
            Event().wait(0.05)
    records = [json.loads(line) for line in request_log.read_text().splitlines()[request_offset:]]
    state_requests = [
        record["req"]
        for record in records
        if record["path"] == "/v1/chat/completions"
        and character in json.dumps(record["req"])
        and "dsm-memory/" in json.dumps(record["req"])
    ]
    assert state_requests, "The state request must reach the HTTP stub"
    review_requests = [
        record["req"]
        for record in records
        if record["path"] == "/v1/chat/completions"
        and record["req"]["messages"][0]["content"].startswith("[dsm-memory/review_")
    ]
    assert len(review_requests) == 3
    aspects = seed_body()["aspects"]
    assert isinstance(aspects, dict)
    for request in review_requests:
        data = json.loads(request["messages"][1]["content"])
        serialized = json.dumps(data).casefold()
        for forbidden in ("aspects", *aspects):
            assert forbidden.casefold() not in serialized
