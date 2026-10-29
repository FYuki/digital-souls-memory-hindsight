"""Real service and ledger checks; only synthetic data enters the IT1 network."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from typing import cast
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.it1
STAMP = "2026-10-28T00:00:00Z"


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=os.environ["SERVICE_URL"], timeout=10, trust_env=False) as value:
        yield value


def create_session(client: httpx.Client, reports_delivery: bool) -> str:
    identifier = "synthetic-" + uuid4().hex
    response = client.post(
        "/sessions",
        json={
            "session_id": identifier,
            "started_at": STAMP,
            "surface": "Web",
            "reports_delivery": reports_delivery,
        },
    )
    assert response.status_code == 201, response.text
    return identifier


def turn_body(session_id: str, **changes: object) -> dict[str, object]:
    return {
        "turn_id": "synthetic-" + uuid4().hex,
        "session_id": session_id,
        "surface": "Web",
        "actor": "char-a",
        "kind": "utterance",
        "text": "  架空のお茶🍵。\n",
        "perceived_by": ["char-a"],
        "addressees": ["char-b"],
        "address_mode": "direct",
        "reply_to_turn_id": None,
        "language": "ja",
        "register": {"一人称": "私"},
        "sensitivity": "public",
        "classifier_version": "fixture-1",
        "occurred_at": STAMP,
        **changes,
    }


def receipt_body() -> dict[str, object]:
    return {"status": "partial", "spoken_until": 2, "reported_by": "Web", "reported_at": STAMP}


def evidence_table(connection: psycopg.Connection[tuple[object, ...]], *columns: str) -> str:
    rows = connection.execute(
        "SELECT table_name, array_agg(column_name::text) FROM information_schema.columns "
        "WHERE table_schema = 'public' GROUP BY table_name"
    ).fetchall()
    matches = [
        str(name) for name, names in rows if isinstance(names, list) and set(columns) <= set(names)
    ]
    assert len(matches) == 1, (columns, matches)
    return matches[0]


def requests_in_configured_process(
    characters: list[str],
    requests: list[tuple[str, str, dict[str, object] | None]],
    home: Path,
) -> list[tuple[int, dict[str, object]]]:
    script = """
import json
import sys
from fastapi.testclient import TestClient
from digital_souls_memory_hindsight.app import app

with TestClient(app) as client:
    results = []
    for method, path, body in json.load(sys.stdin):
        response = client.request(method, path, json=body)
        results.append([response.status_code, response.json()])
print(json.dumps(results, ensure_ascii=False))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(requests),
        env={**os.environ, "EVIDENCE_CHARACTER_IDS": json.dumps(characters), "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return cast(list[tuple[int, dict[str, object]]], json.loads(result.stdout))


@pytest.mark.parametrize(
    ("field", "replacement", "new_status"),
    [("session_id", "absent-session", 404), ("reply_to_turn_id", "absent-turn", 422)],
)
def test_existing_turn_reference_conflicts_preserve_committed_record(
    client: httpx.Client, field: str, replacement: str, new_status: int
) -> None:
    body = turn_body(create_session(client, True))
    turn_id = str(body["turn_id"])
    first = client.post("/turns", json=body)
    assert first.status_code == 201
    candidate = {**body, field: replacement}
    response = client.post("/turns", json=candidate)
    assert response.status_code == 409
    assert response.json() == {"detail": "Evidence already exists"}
    new_id = "synthetic-" + uuid4().hex
    assert client.post("/turns", json={**candidate, "turn_id": new_id}).status_code == new_status
    assert client.get(f"/turns/{new_id}").status_code == 404
    assert client.get(f"/turns/{turn_id}").json() == first.json()
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT turn_id, recorded_at FROM turns WHERE turn_id = ANY(%s)",
            ([turn_id, new_id],),
        ).fetchall()
    assert rows == [(turn_id, datetime.fromisoformat(first.json()["recorded_at"]))]


@pytest.mark.parametrize(
    ("change", "omitted"),
    [("remove-perceiver", False), ("add-actor", False), ("add-actor", True)],
)
def test_retry_after_restarting_with_changed_character_configuration(
    client: httpx.Client, tmp_path: Path, change: str, omitted: bool
) -> None:
    session_id = create_session(client, True)
    if change == "remove-perceiver":
        body = turn_body(
            session_id,
            perceived_by=["char-a", "char-b"],
            address_mode="broadcast",
            addressees=[],
        )
        initial_characters = ["char-a", "char-b"]
        changed_characters = ["char-a"]
    else:
        body = turn_body(
            session_id, actor="user-a", perceived_by=[], address_mode=None, addressees=[]
        )
        if omitted:
            del body["address_mode"]
        initial_characters = ["char-a"]
        changed_characters = ["char-a", "user-a"]
    [(created_status, saved)] = requests_in_configured_process(
        initial_characters, [("POST", "/turns", body)], tmp_path
    )
    assert created_status == 201
    turn_id = str(body["turn_id"])
    new_id = "synthetic-" + uuid4().hex
    results = requests_in_configured_process(
        changed_characters,
        [
            ("POST", "/turns", body),
            ("POST", "/turns", {**body, "turn_id": new_id}),
            ("GET", f"/turns/{turn_id}", None),
            ("GET", f"/turns/{new_id}", None),
        ],
        tmp_path,
    )
    assert results[0][0] == 201
    assert results[0][1] == saved
    assert results[1][0] == 422
    assert results[2][0] == 200
    assert results[2][1] == saved
    assert results[3][0] == 404
    assert client.get(f"/turns/{turn_id}").json() == saved
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT turn_id, recorded_at FROM turns WHERE turn_id = ANY(%s)",
            ([turn_id, new_id],),
        ).fetchall()
    assert rows == [(turn_id, datetime.fromisoformat(str(saved["recorded_at"])))]


def test_service_commits_all_four_records_to_ledger_and_restores_them(client: httpx.Client) -> None:
    session_id = create_session(client, True)
    body = turn_body(session_id)
    turn_id = str(body["turn_id"])
    first = client.post("/turns", json=body)
    assert first.status_code == 201, first.text
    saved = first.json()
    retry = client.post("/turns", json=body)
    assert retry.status_code == 201
    assert retry.json() == saved
    conflict = client.post("/turns", json={**body, "text": "違う本文"})
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "Evidence already exists"}
    receipt = client.post(f"/turns/{turn_id}/receipts", json=receipt_body())
    assert receipt.status_code == 201
    assert receipt.json() == {**receipt_body(), "turn_id": turn_id}
    ending_body = {"ended_at": "2026-10-28T00:01:00Z", "reported_by": "Web"}
    ending = client.post(f"/sessions/{session_id}/endings", json=ending_body)
    assert ending.status_code == 201
    ending_retry = client.post(f"/sessions/{session_id}/endings", json=ending_body)
    assert ending_retry.status_code == 200
    assert ending_retry.json() == ending.json()

    # An independent connection distinguishes committed storage from a process-local cache.
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        for columns, key, identifier in [
            (("session_id", "reports_delivery"), "session_id", session_id),
            (("turn_id", "text"), "turn_id", turn_id),
            (("turn_id", "spoken_until", "reported_by"), "turn_id", turn_id),
            (("session_id", "reported_at", "ended_at"), "session_id", session_id),
        ]:
            table = evidence_table(connection, *columns)
            count = connection.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
                    sql.Identifier(table), sql.Identifier(key)
                ),
                (identifier,),
            ).fetchone()
            assert count == (1,)
        table = evidence_table(connection, "turn_id", "text")
        row = connection.execute(
            sql.SQL("SELECT text FROM {} WHERE turn_id = %s").format(sql.Identifier(table)),
            (turn_id,),
        ).fetchone()
        assert row == (body["text"],)

    with httpx.Client(base_url=os.environ["SERVICE_URL"], timeout=10, trust_env=False) as reader:
        assert reader.get(f"/turns/{turn_id}").json() == saved
        assert reader.get(f"/turns/{turn_id}/receipts").json() == [receipt.json()]
        assert reader.get(f"/sessions/{session_id}/endings").json() == [ending.json()]
        session = reader.get(f"/sessions/{session_id}").json()
        assert session == {
            "session_id": session_id,
            "started_at": STAMP,
            "surface": "Web",
            "reports_delivery": True,
            "ended_at": ending_body["ended_at"],
        }
        assert saved in reader.get("/characters/char-a/turns").json()
        assert turn_id not in [
            turn["turn_id"] for turn in reader.get("/characters/char-b/turns").json()
        ]


@pytest.mark.parametrize("different_text", [False, True])
def test_concurrent_turn_registration_keeps_one_committed_record(
    client: httpx.Client, different_text: bool
) -> None:
    session_id = create_session(client, True)
    first_body = turn_body(session_id)
    second_body = {**first_body, "text": "別の本文"} if different_text else dict(first_body)
    barrier = Barrier(2)

    def send(body: dict[str, object]) -> httpx.Response:
        with httpx.Client(
            base_url=os.environ["SERVICE_URL"], timeout=10, trust_env=False
        ) as sender:
            barrier.wait(timeout=10)
            return sender.post("/turns", json=body)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, [first_body, second_body]))
    assert sorted(r.status_code for r in responses) == (
        [201, 409] if different_text else [201, 201]
    )
    winner = next(r.json() for r in responses if r.status_code == 201)
    if not different_text:
        assert responses[0].json() == responses[1].json()
    turn_id = str(first_body["turn_id"])
    assert client.get(f"/turns/{turn_id}").json() == winner
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        table = evidence_table(connection, "turn_id", "text")
        rows = connection.execute(
            sql.SQL("SELECT text FROM {} WHERE turn_id = %s").format(sql.Identifier(table)),
            (turn_id,),
        ).fetchall()
        assert rows == [(winner["text"],)]


@pytest.mark.parametrize(("reports_delivery", "kind"), [(False, "utterance"), (True, "thought")])
def test_rejected_receipt_leaves_database_and_turn_unchanged(
    client: httpx.Client, reports_delivery: bool, kind: str
) -> None:
    session_id = create_session(client, reports_delivery)
    duplicate = client.post(
        "/sessions",
        json={
            "session_id": session_id,
            "started_at": STAMP,
            "surface": "Web",
            "reports_delivery": not reports_delivery,
        },
    )
    assert duplicate.status_code == 409
    assert client.get(f"/sessions/{session_id}").json()["reports_delivery"] is reports_delivery
    body = turn_body(session_id, kind=kind)
    turn_id = str(body["turn_id"])
    first = client.post("/turns", json=body)
    assert first.status_code == 201, first.text
    response = client.post(f"/turns/{turn_id}/receipts", json=receipt_body())
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid evidence value"}
    assert client.get(f"/turns/{turn_id}/receipts").json() == []
    assert client.get(f"/turns/{turn_id}").json() == first.json()
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        table = evidence_table(connection, "turn_id", "spoken_until", "reported_by")
        row = connection.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE turn_id = %s").format(sql.Identifier(table)),
            (turn_id,),
        ).fetchone()
        assert row == (0,)


@pytest.mark.parametrize(
    ("actor", "perceived_by", "omit_mode", "status"),
    [
        ("char-a", ["char-a"], True, 422),
        ("char-a", [], False, 422),
        ("char-visitor", [], True, 201),
    ],
)
def test_service_uses_configured_character_ids_for_turn_rules(
    client: httpx.Client, actor: str, perceived_by: list[str], omit_mode: bool, status: int
) -> None:
    session_id = create_session(client, True)
    body = turn_body(
        session_id, actor=actor, perceived_by=perceived_by, addressees=[], address_mode="broadcast"
    )
    if omit_mode:
        del body["address_mode"]
    response = client.post("/turns", json=body)
    assert response.status_code == status
    fetched = client.get(f"/turns/{body['turn_id']}")
    assert fetched.status_code == (200 if status == 201 else 404)
    if status == 201:
        assert fetched.json()["address_mode"] == "broadcast"
        assert fetched.json()["perceived_by"] == []


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
@pytest.mark.parametrize("entity", ["session", "turn", "receipt", "ending"])
def test_ledger_rejects_direct_modification_of_saved_evidence(
    client: httpx.Client, operation: str, entity: str
) -> None:
    session_id = create_session(client, True)
    body = turn_body(session_id)
    turn_id = str(body["turn_id"])
    # Parent deletion must fail because it is append-only, not because children hold foreign keys.
    if entity in {"turn", "receipt"}:
        assert client.post("/turns", json=body).status_code == 201
    if entity == "receipt":
        assert client.post(f"/turns/{turn_id}/receipts", json=receipt_body()).status_code == 201
    if entity == "ending":
        assert (
            client.post(
                f"/sessions/{session_id}/endings",
                json={"ended_at": "2026-10-28T00:01:00Z", "reported_by": "Web"},
            ).status_code
            == 201
        )
    columns, key, identifier = {
        "session": (("session_id", "reports_delivery"), "session_id", session_id),
        "turn": (("turn_id", "text"), "turn_id", turn_id),
        "receipt": (("turn_id", "spoken_until", "reported_by"), "turn_id", turn_id),
        "ending": (("session_id", "reported_at", "ended_at"), "session_id", session_id),
    }[entity]
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        table = evidence_table(connection, *columns)
        before = connection.execute(
            sql.SQL("SELECT * FROM {} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(key)
            ),
            (identifier,),
        ).fetchall()
        assert len(before) == 1
        query = (
            sql.SQL("UPDATE {} SET {} = {} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(key), sql.Identifier(key), sql.Identifier(key)
            )
            if operation == "UPDATE"
            else sql.SQL("DELETE FROM {} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(key)
            )
        )
        # A savepoint restores the connection after the expected database rejection.
        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(query, (identifier,))
        after = connection.execute(
            sql.SQL("SELECT * FROM {} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(key)
            ),
            (identifier,),
        ).fetchall()
        assert after == before
