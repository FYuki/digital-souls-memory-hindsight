"""Identity registration and immutable versions against a disposable PostgreSQL ledger."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier
from typing import Protocol, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql

from tests.ut.test_identity_http import seed_body

pytestmark = pytest.mark.it1
ROOT = "/characters/char-a"


class TemperamentValue(Protocol):
    description: str
    version: int


@dataclass
class Renderer:
    error: Exception | None = None
    result: str = "架空の人物は穏やかに好奇心を示します。"
    calls: list[tuple[dict[str, float], str, int, str]] = field(default_factory=list)

    def render_personality(
        self, aspects: dict[str, float], temperament: TemperamentValue, renderer_version: str
    ) -> str:
        self.calls.append(
            (dict(aspects), temperament.description, temperament.version, renderer_version)
        )
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def renderer() -> Renderer:
    return Renderer()


@pytest.fixture
def identity_app(
    database: str, renderer: Renderer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> FastAPI:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_ID", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_TOKEN", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    return cast(FastAPI, module.create_app(llm_gateway=renderer))


@pytest.mark.parametrize(
    ("path", "key", "first", "second"),
    [
        (
            "core-identities",
            "core_identity",
            {"values": ["約束を守る"], "prohibitions": ["傷つけない"]},
            {"values": ["学び続ける"], "prohibitions": []},
        ),
        ("temperaments", "temperament", {"description": "穏やか"}, {"description": "活発"}),
    ],
)
def test_registration_appends_versions_and_preserves_old_values(
    identity_app: FastAPI,
    path: str,
    key: str,
    first: dict[str, object],
    second: dict[str, object],
    database: str,
) -> None:
    client = TestClient(identity_app)
    one = client.post(f"{ROOT}/{path}", json=first)
    two = client.post(f"{ROOT}/{path}", json=second)
    assert one.status_code == two.status_code == 201, (one.text, two.text)
    assert [one.json()["version"], two.json()["version"]] == [1, 2]
    for response, payload in ((one, first), (two, second)):
        for name, value in payload.items():
            assert response.json()[name] == value
        assert response.json()["character_id"] == "char-a"
    assert client.get(f"{ROOT}/{path}/1").json() == one.json()
    assert client.get(f"{ROOT}/{path}/2").json() == two.json()
    assert client.get(f"{ROOT}/identity/current").json()[key] == two.json()
    # A separate connection observes the response's version only after commit.
    table = "core_identities" if key == "core_identity" else "temperaments"
    with psycopg.connect(database, connect_timeout=5) as connection:
        rows = connection.execute(
            sql.SQL("SELECT version FROM {} WHERE character_id=%s ORDER BY version").format(
                sql.Identifier(table)
            ),
            ("char-a",),
        ).fetchall()
    assert rows == [(1,), (2,)]
    other = client.post(f"/characters/other_A-1/{path}", json=first)
    assert other.status_code == 201, other.text
    assert other.json()["version"] == 1


def test_current_is_null_until_registered_and_missing_versions_are_404(
    identity_app: FastAPI,
) -> None:
    client = TestClient(identity_app)
    current = client.get(f"{ROOT}/identity/current")
    assert current.status_code == 200
    assert current.json() == {
        "core_identity": None,
        "temperament": None,
        "personality_profile": None,
    }
    for path in ("core-identities", "temperaments", "personality-profiles"):
        assert client.get(f"{ROOT}/{path}/1").status_code == 404


def test_author_seed_uses_latest_temperament_once_and_never_exposes_aspects(
    identity_app: FastAPI,
    renderer: Renderer,
    database: str,
) -> None:
    client = TestClient(identity_app)
    for description in ("古い架空の気質", "生成に使う架空の気質"):
        assert (
            client.post(f"{ROOT}/temperaments", json={"description": description}).status_code
            == 201
        )
    seeded = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert seeded.status_code == 201, seeded.text
    expected = {
        "character_id": "char-a",
        "version": 1,
        "description": renderer.result,
        "temperament_version": 2,
        "renderer_version": "fixture-renderer-1",
    }
    assert seeded.json() == expected
    assert renderer.calls == [
        (seed_body()["aspects"], "生成に使う架空の気質", 2, "fixture-renderer-1")
    ]
    assert client.get(f"{ROOT}/personality-profiles/1").json() == expected
    assert (
        client.post(f"{ROOT}/temperaments", json={"description": "更新された気質"}).status_code
        == 201
    )
    current = client.get(f"{ROOT}/identity/current").json()
    assert current["temperament"]["version"] == 3
    assert current["personality_profile"] == expected
    duplicate = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert duplicate.status_code == 409
    assert len(renderer.calls) == 1
    with psycopg.connect(database, connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT version, aspects, temperament_version, renderer_version "
            "FROM personality_profiles WHERE character_id='char-a'"
        ).fetchall()
    assert rows == [(1, seed_body()["aspects"], 2, "fixture-renderer-1")]


def test_seed_without_temperament_is_404_and_does_not_render_or_save(
    identity_app: FastAPI,
    renderer: Renderer,
    database: str,
) -> None:
    response = TestClient(identity_app).post(
        f"{ROOT}/personality-profiles/author-seed", json=seed_body()
    )
    assert response.status_code == 404
    assert renderer.calls == []
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM personality_profiles").fetchone() == (0,)


@pytest.mark.parametrize("failure", ["exception", "blank", "unconfigured"])
def test_failed_generation_is_unpublished_and_retry_can_create_first_version(
    identity_app: FastAPI,
    renderer: Renderer,
    database: str,
    failure: str,
) -> None:
    client = TestClient(identity_app)
    assert client.post(f"{ROOT}/temperaments", json={"description": "穏やか"}).status_code == 201
    renderer.error = RuntimeError("synthetic renderer failure") if failure == "exception" else None
    renderer.result = " \t " if failure == "blank" else "生成済みの架空の記述"
    if failure == "unconfigured":
        module = importlib.import_module("digital_souls_memory_hindsight.app")
        failing_client = TestClient(module.create_app())
    else:
        failing_client = client
    failed = failing_client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert failed.status_code == 503
    assert client.get(f"{ROOT}/identity/current").json()["personality_profile"] is None
    assert client.get(f"{ROOT}/personality-profiles/1").status_code == 404
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute("SELECT count(*) FROM personality_profiles").fetchone() == (0,)
    renderer.error = None
    renderer.result = "成功した架空の記述"
    recovered = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["version"] == 1


@pytest.mark.parametrize("path", ["core-identities", "temperaments"])
def test_concurrent_registration_allocates_distinct_contiguous_versions(
    identity_app: FastAPI,
    path: str,
) -> None:
    count = 6
    barrier = Barrier(count)

    def register(index: int) -> tuple[int, dict[str, object]]:
        body: dict[str, object] = (
            {"values": [str(index)], "prohibitions": []}
            if path == "core-identities"
            else {"description": str(index)}
        )
        client = TestClient(identity_app)
        barrier.wait(timeout=15)
        response = client.post(f"{ROOT}/{path}", json=body)
        assert response.status_code == 201, response.text
        return int(response.json()["version"]), body

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(register, range(count)))
    assert {version for version, _ in results} == set(range(1, count + 1))
    client = TestClient(identity_app)
    for version, payload in results:
        saved = client.get(f"{ROOT}/{path}/{version}").json()
        for name, value in payload.items():
            assert saved[name] == value


def test_concurrent_author_seeds_commit_only_one_profile(
    identity_app: FastAPI,
    renderer: Renderer,
    database: str,
) -> None:
    assert (
        TestClient(identity_app)
        .post(f"{ROOT}/temperaments", json={"description": "穏やか"})
        .status_code
        == 201
    )
    barrier = Barrier(2)

    def seed(_: int) -> int:
        barrier.wait(timeout=15)
        return (
            TestClient(identity_app)
            .post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
            .status_code
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(seed, range(2)))
    assert sorted(statuses) == [201, 409]
    assert len(renderer.calls) == 1
    with psycopg.connect(database, connect_timeout=5) as connection:
        assert connection.execute("SELECT version FROM personality_profiles").fetchall() == [(1,)]


@pytest.mark.parametrize("table", ["core_identities", "temperaments", "personality_profiles"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_identity_rows_cannot_be_changed_by_direct_sql(
    identity_app: FastAPI,
    database: str,
    table: str,
    operation: str,
) -> None:
    client = TestClient(identity_app)
    if table == "core_identities":
        response = client.post(f"{ROOT}/core-identities", json={"values": [], "prohibitions": []})
        assert response.status_code == 201
    else:
        assert (
            client.post(f"{ROOT}/temperaments", json={"description": "穏やか"}).status_code == 201
        )
        if table == "personality_profiles":
            response = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
            assert response.status_code == 201
    before = client.get(f"{ROOT}/identity/current").json()
    statement = {
        "UPDATE": "UPDATE {} SET version=99",
        "DELETE": "DELETE FROM {}",
        "TRUNCATE": "TRUNCATE {} CASCADE",
    }[operation]
    with psycopg.connect(database, connect_timeout=5) as connection:
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                connection.execute(sql.SQL(statement).format(sql.Identifier(table)))
    assert client.get(f"{ROOT}/identity/current").json() == before
