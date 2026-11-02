"""Character classification is explicit configuration, separate from database availability."""

import importlib
from pathlib import Path
from typing import Protocol, cast

import pytest

pytestmark = pytest.mark.ut


class Settings(Protocol):
    character_ids: frozenset[str] | None
    delivery_timeout_seconds: int
    hindsight_url: str | None
    recall_min_reranker_score: float
    author_id: str | None
    author_token: str | None


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


@pytest.mark.parametrize(("raw", "expected"), [(None, 600), ("42", 42)])
def test_delivery_timeout_default_and_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str | None, expected: int
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    if raw is None:
        monkeypatch.delenv("EVIDENCE_DELIVERY_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("EVIDENCE_DELIVERY_TIMEOUT_SECONDS", raw)
    module = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    assert module.load_settings().delivery_timeout_seconds == expected


@pytest.mark.parametrize("raw", ["", " ", "invalid", "0", "-1", "1.5"])
def test_delivery_timeout_rejects_invalid_or_nonpositive_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("EVIDENCE_DELIVERY_TIMEOUT_SECONDS", raw)
    module = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    with pytest.raises(ValueError):
        module.load_settings()


@pytest.mark.parametrize("raw", [None, "http://hindsight:8888", "", "   "])
def test_hindsight_url_is_optional_for_migrations_but_cannot_be_empty(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    if raw is None:
        monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    else:
        monkeypatch.setenv("HINDSIGHT_URL", raw)
    module = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    if raw is not None and not raw.strip():
        with pytest.raises(ValueError):
            module.load_settings()
    else:
        assert module.load_settings().hindsight_url == raw


@pytest.mark.parametrize("raw", [None, "0", "0.73", "1"])
def test_recall_threshold_default_and_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str | None
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    if raw is None:
        monkeypatch.delenv("RECALL_MIN_RERANKER_SCORE", raising=False)
    else:
        monkeypatch.setenv("RECALL_MIN_RERANKER_SCORE", raw)
    module = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    value = module.load_settings().recall_min_reranker_score
    if raw is None:
        # The numerical default remains provisional until IT2 calibration.
        assert 0 < value <= 1
    else:
        assert value == float(raw)


@pytest.mark.parametrize("raw", ["", " ", "invalid", "nan", "inf", "-inf", "-0.1", "1.1"])
def test_recall_threshold_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("RECALL_MIN_RERANKER_SCORE", raw)
    module = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    with pytest.raises(ValueError):
        module.load_settings()


@pytest.mark.parametrize("name", ["MEMORY_AUTHOR_ID", "MEMORY_AUTHOR_TOKEN"])
@pytest.mark.parametrize("raw", [None, "", " \t "])
def test_missing_author_setting_is_unavailable_without_preventing_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, raw: str | None
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-token")
    if raw is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, raw)
    module = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    settings = module.load_settings()
    assert (settings.author_id if name == "MEMORY_AUTHOR_ID" else settings.author_token) is None


def test_author_settings_are_loaded_without_exposing_token_in_repr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-token")
    module = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    settings = module.load_settings()
    assert settings.author_id == "synthetic-author"
    assert settings.author_token == "synthetic-token"
    assert "synthetic-token" not in repr(settings)
