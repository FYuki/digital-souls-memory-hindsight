"""Run inside the IT1 test container; connection settings must be supplied by Compose."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.it1
ROOT = Path(__file__).resolve().parents[2]


def test_databases_are_distinct_on_the_same_postgres_instance() -> None:
    import psycopg

    ledger_url = os.environ["DATABASE_URL"]
    hindsight_url = os.environ["HINDSIGHT_DATABASE_URL"]
    query = """
        SELECT current_database(), system_identifier::text,
               inet_server_addr()::text, inet_server_port()
        FROM pg_control_system()
    """
    with psycopg.connect(ledger_url, connect_timeout=5) as ledger:
        ledger_identity = ledger.execute(query).fetchone()
    with psycopg.connect(hindsight_url, connect_timeout=5) as hindsight:
        hindsight_identity = hindsight.execute(query).fetchone()
    assert ledger_identity is not None and hindsight_identity is not None
    assert ledger_identity[0] != hindsight_identity[0]
    assert ledger_identity[1:] == hindsight_identity[1:]


def test_postgres_18_provides_required_extensions() -> None:
    import psycopg

    with psycopg.connect(os.environ["HINDSIGHT_DATABASE_URL"], connect_timeout=5) as connection:
        version = connection.execute("SHOW server_version_num").fetchone()
        assert version is not None
        assert 180000 <= int(version[0]) < 190000
        available: dict[str, str] = dict(
            connection.execute(
                "SELECT name, default_version FROM pg_available_extensions "
                "WHERE name IN ('vector', 'pgroonga', 'pg_bigm')"
            ).fetchall()
        )
        assert available["vector"] == "0.8.2"
        assert available["pgroonga"] == "4.1.0"
        assert "pg_bigm" in available
        installed: dict[str, str] = dict(
            connection.execute(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('vector', 'pgroonga')"
            ).fetchall()
        )
        assert installed == {"vector": "0.8.2", "pgroonga": "4.1.0"}


def test_alembic_upgrade_records_head_only_in_ledger_database() -> None:
    import psycopg
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # Hindsight owns a separate Alembic history; only its existing revisions must remain.
    with psycopg.connect(os.environ["HINDSIGHT_DATABASE_URL"], connect_timeout=5) as hindsight:
        before = hindsight.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    assert head is not None
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as ledger:
        assert ledger.execute("SELECT version_num FROM alembic_version").fetchall() == [(head,)]
    with psycopg.connect(os.environ["HINDSIGHT_DATABASE_URL"], connect_timeout=5) as hindsight:
        after = hindsight.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
    assert after == before
    assert (head,) not in after


def test_service_health_over_container_network() -> None:
    httpx = importlib.import_module("httpx")
    response = httpx.get(os.environ["SERVICE_URL"] + "/health", timeout=5, trust_env=False)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_container_launch_environment_imports_src_outside_pytest(tmp_path: Path) -> None:
    environment = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import digital_souls_memory_hindsight as package; print(package.__file__)",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve().is_relative_to(ROOT / "src")
