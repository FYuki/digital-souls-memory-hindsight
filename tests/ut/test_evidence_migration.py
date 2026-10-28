"""The ledger must be installable without a database or character configuration."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]


def test_upgrade_generates_four_evidence_tables(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "head",
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
    tables = re.findall(r"CREATE TABLE\s+(\w+)", result.stdout, flags=re.IGNORECASE)
    assert len([table for table in tables if table != "alembic_version"]) == 4
