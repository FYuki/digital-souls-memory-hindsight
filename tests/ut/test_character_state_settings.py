"""State limits are explicit, configurable positive integers."""

from pathlib import Path

import pytest

from digital_souls_memory_hindsight.settings import load_settings

pytestmark = pytest.mark.ut
TTL = "CHARACTER_STATE_TTL_SECONDS"
CHARS = "CHARACTER_STATE_INPUT_MAX_CHARS"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    monkeypatch.delenv(TTL, raising=False)
    monkeypatch.delenv(CHARS, raising=False)


def test_default_state_limits_are_thirty_minutes_and_four_thousand_codepoints() -> None:
    settings = load_settings()
    assert settings.character_state_ttl_seconds == 1800
    assert settings.character_state_input_max_chars == 4000


def test_state_limits_can_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TTL, "42")
    monkeypatch.setenv(CHARS, "17")
    settings = load_settings()
    assert settings.character_state_ttl_seconds == 42
    assert settings.character_state_input_max_chars == 17


@pytest.mark.parametrize("key", [TTL, CHARS])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid", ""])
def test_invalid_state_limits_are_rejected(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        load_settings()
