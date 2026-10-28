import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_initial_revision_offline_sql_only_changes_migration_history(
    tmp_path: Path, operation: str
) -> None:
    target = "head" if operation == "upgrade" else "head:base"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            operation,
            target,
            "--sql",
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "PYTHONPATH": str(ROOT / "src"),
            "DATABASE_URL": "postgresql://it1@postgres:5432/synthetic_ledger",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "alembic_version" in result.stdout
    tables = re.findall(r"(?:CREATE|DROP|ALTER) TABLE\s+(\w+)", result.stdout)
    assert tables == ["alembic_version"]
