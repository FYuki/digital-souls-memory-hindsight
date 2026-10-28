import asyncio
import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]


class Settings(Protocol):
    database_url: str


class SettingsModule(Protocol):
    def load_settings(self) -> Settings: ...


def test_settings_load_database_url_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    url = "postgresql://it1@postgres:5432/synthetic_ledger"
    monkeypatch.setenv("DATABASE_URL", url)
    settings = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    assert settings.load_settings().database_url == url


@pytest.mark.parametrize("value", [None, "", "   "])
def test_settings_reject_missing_or_empty_database_url(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", value)
    settings = cast(
        SettingsModule, importlib.import_module("digital_souls_memory_hindsight.settings")
    )
    with pytest.raises((KeyError, ValueError)):
        settings.load_settings()


def test_health_returns_ok_without_database_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://it1@postgres:5432/synthetic_ledger")
    app = importlib.import_module("digital_souls_memory_hindsight.app").app
    httpx = importlib.import_module("httpx")

    async def request_health() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://service"
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    asyncio.run(request_health())


def test_src_import_succeeds_in_separate_python_with_launch_environment(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import digital_souls_memory_hindsight as package; print(package.__file__)",
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve().is_relative_to(ROOT / "src")
