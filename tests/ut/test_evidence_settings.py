"""Character classification is explicit configuration, separate from database availability."""

import importlib
from pathlib import Path
from typing import Protocol, cast

import pytest

pytestmark = pytest.mark.ut


class Settings(Protocol):
    character_ids: frozenset[str] | None


class SettingsModule(Protocol):
    def load_settings(self) -> Settings: ...


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["char-a", "char-b"]', frozenset({"char-a", "char-b"})),
        ("[]", frozenset()),
        (None, None),
        ("", None),
        ("not-json", None),
        ("null", None),
        ('"char-a"', None),
        ('{"char-a": true}', None),
        ('[" "]', None),
        ("[1]", None),
    ],
)
def test_character_configuration_distinguishes_empty_registry_from_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: str | None,
    expected: frozenset[str] | None,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    if raw is None:
        monkeypatch.delenv("EVIDENCE_CHARACTER_IDS", raising=False)
    else:
        monkeypatch.setenv("EVIDENCE_CHARACTER_IDS", raw)
    settings = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    assert settings.load_settings().character_ids == expected
