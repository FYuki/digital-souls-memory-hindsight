"""Identity rules through HTTP with transactional storage and generation doubles."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory_hindsight.evidence.service import EvidenceService
from digital_souls_memory_hindsight.identity.ports import (
    CoreIdentity,
    IdentitySnapshot,
    IdentityTransaction,
    IdentityUnavailable,
    PersonalityProfile,
    Temperament,
)
from tests.consistency_doubles import Gateway, Reviewer
from tests.ut.test_evidence_http import MemoryStore, session_body, turn_body
from tests.ut.test_identity_http import seed_body

pytestmark = pytest.mark.ut
ROOT = "/characters/char-a"


@dataclass
class IdentityRows:
    cores: dict[int, CoreIdentity] = field(default_factory=dict)
    temperaments: dict[int, Temperament] = field(default_factory=dict)
    profiles: dict[int, PersonalityProfile] = field(default_factory=dict)

    def get_core(self, version: int | None) -> CoreIdentity | None:
        return self.cores.get(version if version is not None else max(self.cores, default=0))

    def get_temperament(self, version: int | None) -> Temperament | None:
        return self.temperaments.get(
            version if version is not None else max(self.temperaments, default=0)
        )

    def get_profile(self, version: int | None) -> PersonalityProfile | None:
        return self.profiles.get(version if version is not None else max(self.profiles, default=0))

    def add_core(self, value: CoreIdentity) -> None:
        self.cores[value.version] = value

    def add_temperament(self, value: Temperament) -> None:
        self.temperaments[value.version] = value

    def add_profile(self, value: PersonalityProfile) -> None:
        self.profiles[value.version] = value


@dataclass
class IdentityStore:
    characters: dict[str, IdentityRows] = field(default_factory=dict)
    fail_commit: bool = False
    active_character: str | None = None

    def _rows(self, character: str) -> IdentityRows:
        return self.characters.get(character, IdentityRows())

    @contextmanager
    def transaction(self, character_id: str) -> Iterator[IdentityTransaction]:
        previous = self._rows(character_id)
        pending = replace(
            previous,
            cores=previous.cores.copy(),
            temperaments=previous.temperaments.copy(),
            profiles=previous.profiles.copy(),
        )
        self.active_character = character_id
        try:
            yield pending
            if self.fail_commit:
                raise IdentityUnavailable()
            self.characters[character_id] = pending
        finally:
            self.active_character = None

    def current(self, character_id: str) -> IdentitySnapshot:
        rows = self._rows(character_id)
        return IdentitySnapshot(
            rows.get_core(None), rows.get_temperament(None), rows.get_profile(None)
        )

    def get_core(self, character_id: str, version: int) -> CoreIdentity | None:
        return self._rows(character_id).get_core(version)

    def get_temperament(self, character_id: str, version: int) -> Temperament | None:
        return self._rows(character_id).get_temperament(version)

    def get_profile(self, character_id: str, version: int) -> PersonalityProfile | None:
        return self._rows(character_id).get_profile(version)


@dataclass
class Renderer(Gateway):
    store: IdentityStore
    result: str = "架空の人格の生成された記述"
    error: Exception | None = None
    calls: list[tuple[dict[str, float], Temperament, str]] = field(default_factory=list)

    def render_personality(
        self, aspects: dict[str, float], temperament: Temperament, renderer_version: str
    ) -> str:
        assert self.store.active_character == temperament.character_id
        self.calls.append((dict(aspects), temperament, renderer_version))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def store() -> IdentityStore:
    return IdentityStore()


@pytest.fixture
def renderer(store: IdentityStore) -> Renderer:
    return Renderer(store)


@pytest.fixture
def client(
    store: IdentityStore, renderer: Renderer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> TestClient:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", '["char-a"]')
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_ID", raising=False)
    monkeypatch.delenv("MEMORY_AUTHOR_TOKEN", raising=False)
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    monkeypatch.setattr(module, "PostgresIdentityStore", lambda url: store)
    return TestClient(module.create_app(llm_gateway=renderer, reviewer=Reviewer()))


@pytest.mark.parametrize(
    ("path", "key", "first", "second"),
    [
        (
            "core-identities",
            "core_identity",
            {"values": ["約束"], "prohibitions": ["攻撃"]},
            {"values": [], "prohibitions": []},
        ),
        ("temperaments", "temperament", {"description": "穏やか"}, {"description": "活発"}),
    ],
)
def test_identity_registration_appends_and_reads_versions(
    client: TestClient, path: str, key: str, first: dict[str, object], second: dict[str, object]
) -> None:
    one = client.post(f"{ROOT}/{path}", json=first)
    two = client.post(f"{ROOT}/{path}", json=second)
    assert one.status_code == two.status_code == 201
    assert [one.json()["version"], two.json()["version"]] == [1, 2]
    assert client.get(f"{ROOT}/{path}/1").json() == {
        k: v for k, v in one.json().items() if k != "review"
    }
    for name, value in first.items():
        assert one.json()[name] == value
    for name, value in second.items():
        assert two.json()[name] == value
    assert client.get(f"{ROOT}/identity/current").json()[key] == {
        k: v for k, v in two.json().items() if k != "review"
    }
    other = client.post(f"/characters/other_A-1/{path}", json=first)
    assert other.status_code == 201
    assert other.json()["version"] == 1


def test_missing_identity_reads_are_null_or_404(client: TestClient) -> None:
    response = client.get(f"{ROOT}/identity/current")
    assert response.status_code == 200
    assert response.json() == {
        "core_identity": None,
        "temperament": None,
        "personality_profile": None,
    }
    for path in ("core-identities", "temperaments", "personality-profiles"):
        for version in (-1, 0, 12):
            assert client.get(f"{ROOT}/{path}/{version}").status_code == 404


def test_seed_renders_latest_temperament_once_and_preserves_source_version(
    client: TestClient,
    renderer: Renderer,
) -> None:
    for description in ("古い気質", "生成元の気質"):
        assert (
            client.post(f"{ROOT}/temperaments", json={"description": description}).status_code
            == 201
        )
    response = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 201
    expected = {
        "character_id": "char-a",
        "version": 1,
        "description": renderer.result,
        "temperament_version": 2,
        "renderer_version": "fixture-renderer-1",
    }
    assert response.json() == {
        **expected,
        "review": {"consistent": True, "reason": "架空の審査理由"},
    }
    assert len(renderer.calls) == 1
    aspects, temperament, renderer_version = renderer.calls[0]
    assert aspects == seed_body()["aspects"]
    assert (temperament.description, temperament.version, renderer_version) == (
        "生成元の気質",
        2,
        "fixture-renderer-1",
    )
    assert (
        client.post(f"{ROOT}/temperaments", json={"description": "新しい気質"}).status_code == 201
    )
    current = client.get(f"{ROOT}/identity/current").json()
    assert current["temperament"]["version"] == 3
    assert current["personality_profile"] == expected
    assert client.get(f"{ROOT}/personality-profiles/1").json() == expected
    assert (
        client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body()).status_code == 409
    )
    assert len(renderer.calls) == 1


def test_seed_requires_temperament_without_rendering(
    client: TestClient, renderer: Renderer
) -> None:
    response = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 404
    assert renderer.calls == []
    assert client.get(f"{ROOT}/identity/current").json()["personality_profile"] is None


@pytest.mark.parametrize("failure", ["exception", "blank", "commit"])
def test_failed_seed_is_unpublished_and_retry_starts_at_version_one(
    client: TestClient,
    renderer: Renderer,
    store: IdentityStore,
    failure: str,
) -> None:
    assert client.post(f"{ROOT}/temperaments", json={"description": "穏やか"}).status_code == 201
    renderer.error = (
        RuntimeError("synthetic generation failure") if failure == "exception" else None
    )
    renderer.result = " \t " if failure == "blank" else "架空の生成結果"
    store.fail_commit = failure == "commit"
    response = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 503
    assert store.current("char-a").personality_profile is None
    assert client.get(f"{ROOT}/identity/current").json()["personality_profile"] is None
    assert client.get(f"{ROOT}/personality-profiles/1").status_code == 404
    renderer.error = None
    renderer.result = "再試行した架空の記述"
    store.fail_commit = False
    recovered = client.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert recovered.status_code == 201
    assert recovered.json()["version"] == 1


def test_unconfigured_gateway_saves_nothing(
    client: TestClient,
    store: IdentityStore,
) -> None:
    assert client.post(f"{ROOT}/temperaments", json={"description": "穏やか"}).status_code == 201
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    unconfigured = TestClient(module.create_app())
    response = unconfigured.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body())
    assert response.status_code == 503
    assert store.current("char-a").personality_profile is None
    assert unconfigured.get(f"{ROOT}/identity/current").json()["personality_profile"] is None


@pytest.mark.parametrize("path", ["core-identities", "temperaments"])
def test_failed_identity_commit_returns_503_without_consuming_a_version(
    client: TestClient,
    store: IdentityStore,
    path: str,
) -> None:
    body: dict[str, object] = (
        {"values": [], "prohibitions": []}
        if path == "core-identities"
        else {"description": "穏やか"}
    )
    store.fail_commit = True
    assert client.post(f"{ROOT}/{path}", json=body).status_code == 503
    assert client.get(f"{ROOT}/{path}/1").status_code == 404
    store.fail_commit = False
    response = client.post(f"{ROOT}/{path}", json=body)
    assert response.status_code == 201
    assert response.json()["version"] == 1


def test_app_factories_keep_gateway_and_ledger_scoped(
    client: TestClient,
    renderer: Renderer,
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    first = module.create_app(llm_gateway=renderer, reviewer=Reviewer())
    second = module.create_app()
    first.state.ledger_service = EvidenceService(MemoryStore(), frozenset({"char-a"}))
    second.state.ledger_service = EvidenceService(MemoryStore(), frozenset({"char-a"}))
    a, b = TestClient(first), TestClient(second)
    assert a.post("/sessions", json=session_body()).status_code == 201
    assert a.post("/turns", json=turn_body()).status_code == 201
    assert a.get("/turns/t-1").status_code == 200
    assert b.get("/turns/t-1").status_code == 404
    assert a.post(f"{ROOT}/temperaments", json={"description": "穏やか"}).status_code == 201
    assert b.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body()).status_code == 503
    assert a.post(f"{ROOT}/personality-profiles/author-seed", json=seed_body()).status_code == 201
