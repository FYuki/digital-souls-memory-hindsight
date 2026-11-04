"""Resident reference propagation and rejection through the recall HTTP boundary."""

import importlib
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from tests.ut.test_recall_http import application, recall_body, start

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("reference", [None, "resident-1"])
@pytest.mark.parametrize("use_turn", [False, True])
def test_recall_passes_optional_resident_reference_to_saved_record(
    reference: str | None, use_turn: bool
) -> None:
    h = application()
    with TestClient(h.app) as client:
        start(client)
        h.turn(client)
        body: dict[str, object] = (
            {"session_id": "s-1", "format": "materials", "turn_id": "t-1"}
            if use_turn
            else recall_body()
        )
        body["resident_provision_id"] = reference
        response = client.post("/characters/char-a/recall", json=body)
    assert response.status_code == 200, response.text
    record = asdict(h.provisions.records[response.json()["provision_id"]])
    assert record["resident_provision_id"] == reference
    assert record["temperament_version"] is record["personality_version"] is None
    assert "resident_provision_id" not in response.json()


def test_invalid_resident_reference_becomes_422_without_saving() -> None:
    ports = importlib.import_module("digital_souls_memory_hindsight.recall.ports")
    h = application()
    h.provisions.error = ports.ResidentProvisionNotFound()
    with TestClient(h.app) as client:
        start(client)
        response = client.post(
            "/characters/char-a/recall", json=recall_body(resident_provision_id="missing")
        )
        assert response.status_code == 422
        assert h.provisions.records == {}
        h.provisions.error = None
        recovered = client.post("/characters/char-a/recall", json=recall_body())
    assert recovered.status_code == 200, recovered.text
    assert set(h.provisions.records) == {recovered.json()["provision_id"]}


@pytest.mark.parametrize("reference", ["", " \t ", 42, True])
def test_recall_rejects_malformed_resident_reference_without_search(reference: object) -> None:
    h = application()
    with TestClient(h.app) as client:
        start(client)
        response = client.post(
            "/characters/char-a/recall", json=recall_body(resident_provision_id=reference)
        )
    assert response.status_code == 422
    assert h.memory.calls == []
    assert h.provisions.records == {}
