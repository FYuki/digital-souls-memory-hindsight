"""LLM configuration is complete or absent, with secrets kept out of diagnostics."""

import importlib
from pathlib import Path

import pytest

from digital_souls_memory_hindsight.settings import load_settings

pytestmark = pytest.mark.ut
LLM_ENV = {
    "MEMORY_LLM_BASE_URL": "http://synthetic-llm:8080/v1",
    "MEMORY_LLM_MODEL": "synthetic-model",
    "MEMORY_LLM_API_KEY": "synthetic-secret-llm-token",
    "MEMORY_LLM_TIMEOUT_SECONDS": "7.5",
}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    for key in LLM_ENV:
        monkeypatch.delenv(key, raising=False)


def test_absent_llm_configuration_is_disabled() -> None:
    assert load_settings().llm is None


def test_complete_configuration_loads_all_values_and_hides_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in LLM_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    assert settings.llm is not None
    assert settings.llm.base_url == LLM_ENV["MEMORY_LLM_BASE_URL"]
    assert settings.llm.model == LLM_ENV["MEMORY_LLM_MODEL"]
    assert settings.llm.api_key == LLM_ENV["MEMORY_LLM_API_KEY"]
    assert settings.llm.timeout_seconds == 7.5
    for value in (repr(settings), repr(settings.llm)):
        assert LLM_ENV["MEMORY_LLM_API_KEY"].casefold() not in value.casefold()


@pytest.mark.parametrize("mask", range(1, 15))
def test_every_partial_configuration_fails_at_app_creation(
    monkeypatch: pytest.MonkeyPatch, mask: int
) -> None:
    module = importlib.import_module("digital_souls_memory_hindsight.app")
    for index, (key, value) in enumerate(LLM_ENV.items()):
        if mask & (1 << index):
            monkeypatch.setenv(key, value)
    with pytest.raises(ValueError) as error:
        module.create_app()
    assert LLM_ENV["MEMORY_LLM_API_KEY"] not in str(error.value)


@pytest.mark.parametrize("key", LLM_ENV)
@pytest.mark.parametrize("blank", ["", " \t "])
def test_blank_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch, key: str, blank: str
) -> None:
    for name, value in LLM_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(key, blank)
    with pytest.raises(ValueError):
        load_settings()


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf", "not-a-number"])
def test_timeout_must_be_positive_and_finite(monkeypatch: pytest.MonkeyPatch, timeout: str) -> None:
    for name, value in LLM_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValueError):
        load_settings()


@pytest.mark.parametrize("url", ["ftp://synthetic-llm/v1", "synthetic-llm", "http://"])
def test_base_url_requires_http_endpoint(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    for name, value in LLM_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", url)
    with pytest.raises(ValueError):
        load_settings()
