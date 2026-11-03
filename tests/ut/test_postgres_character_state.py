"""Storage failures must not masquerade as absent state."""

import importlib
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory_hindsight.character_state.domain import CharacterState
from digital_souls_memory_hindsight.character_state.ports import StateJob, StoredState
from digital_souls_memory_hindsight.evidence.domain import StateKey
from digital_souls_memory_hindsight.evidence.ports import EvidenceUnavailable
from digital_souls_memory_hindsight.postgres_character_state import PostgresCharacterStateStore
from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut


def test_state_migration_extends_profile_as_the_only_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0015_character_states"]
    revision = scripts.get_revision("0015_character_states")
    assert revision is not None
    assert revision.down_revision == "0014_character_profiles"


@pytest.mark.parametrize("operation", ["get", "claim"])
def test_connect_failure_is_evidence_unavailable(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_character_state")
    monkeypatch.setattr(psycopg, "connect", MagicMock(side_effect=psycopg.OperationalError()))
    store = module.PostgresCharacterStateStore("synthetic-db")
    with pytest.raises(EvidenceUnavailable):
        if operation == "get":
            store.get(StateKey("char-a", "s-1", "Web"))
        else:
            store.claim(NOW, timedelta(seconds=60))


def test_save_rejects_job_without_a_current_generation_and_lease_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.postgres_character_state")
    domain = importlib.import_module("digital_souls_memory_hindsight.character_state.domain")
    ports = importlib.import_module("digital_souls_memory_hindsight.character_state.ports")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    job = ports.StateJob(
        key=StateKey("char-a", "s-1", "Web"),
        generation=1,
        attempts=1,
        lease_token="synthetic-lease",
        lease_until=NOW + timedelta(seconds=60),
    )
    store = module.PostgresCharacterStateStore("synthetic-db")
    assert store.save(job, domain.CharacterState("穏やか", "温かい", "お茶"), NOW) is False


def job() -> StateJob:
    return StateJob(
        StateKey("char-a", "s-1", "Web"), 2, 1, "synthetic-lease", NOW + timedelta(seconds=60)
    )


@pytest.mark.parametrize("matched", [False, True])
@pytest.mark.parametrize("operation", ["complete", "defer", "fail"])
def test_finishing_job_requires_current_generation_and_live_lease(
    monkeypatch: pytest.MonkeyPatch, matched: bool, operation: str
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (2,) if matched else None
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    store = PostgresCharacterStateStore("synthetic-db")
    item = job()
    if operation == "complete":
        result = store.complete(item, NOW)
    elif operation == "defer":
        result = store.defer(item, NOW, NOW + timedelta(seconds=1), "gateway_unavailable")
    else:
        result = store.fail(item, NOW, "invalid_output")
    assert result is matched
    assert connection.execute.call_args.args[1][-6:] == (
        *item.key,
        item.generation,
        item.lease_token,
        NOW,
    )


@pytest.mark.parametrize("generated", [False, True])
def test_get_restores_current_and_saved_generation_together(
    monkeypatch: pytest.MonkeyPatch, generated: bool
) -> None:
    state = CharacterState("穏やか", "温かい", "お茶")
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (
        (2, 1, state.mood, state.relationship_temperature, state.current_topic, NOW)
        if generated
        else (2, None, None, None, None, None)
    )
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    assert PostgresCharacterStateStore("synthetic-db").get(job().key) == StoredState(
        2, 1 if generated else None, state if generated else None, NOW if generated else None
    )


def test_claim_restores_key_generation_and_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    item = job()
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (
        *item.key,
        item.generation,
        item.attempts,
        item.lease_token,
        item.lease_until,
    )
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    assert PostgresCharacterStateStore("synthetic-db").claim(NOW, timedelta(seconds=60)) == item


@pytest.mark.parametrize("operation", ["save", "complete", "defer", "fail"])
def test_write_error_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.side_effect = psycopg.OperationalError("synthetic failure")
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    store = PostgresCharacterStateStore("synthetic-db")
    with pytest.raises(EvidenceUnavailable):
        if operation == "save":
            store.save(job(), CharacterState("穏やか", "温かい", "お茶"), NOW)
        elif operation == "complete":
            store.complete(job(), NOW)
        elif operation == "defer":
            store.defer(job(), NOW, NOW + timedelta(seconds=1), "gateway_unavailable")
        else:
            store.fail(job(), NOW, "invalid_output")
