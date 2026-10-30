"""The ledger must be installable without a database or character configuration."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_deletion_execution_revision_generates_reversible_storage_changes(
    tmp_path: Path, operation: str
) -> None:
    target = (
        "0009_deletion_requests:0010_deletion_execution"
        if operation == "upgrade"
        else "0010_deletion_execution:0009_deletion_requests"
    )
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
    verb = "CREATE" if operation == "upgrade" else "DROP"
    tables = set(re.findall(rf"{verb} TABLE\s+(\w+)", result.stdout, re.IGNORECASE))
    assert {"turn_tombstones", "deletion_outbox"} <= tables
    assert "ALTER TABLE outbox" in result.stdout


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_deletion_revision_offline_changes_only_deletion_storage(
    tmp_path: Path, operation: str
) -> None:
    target = (
        "0008_provision_trust:0009_deletion_requests"
        if operation == "upgrade"
        else "0009_deletion_requests:0008_provision_trust"
    )
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
    changes = re.findall(r"(?:CREATE|DROP|ALTER) TABLE\s+(\w+)", result.stdout)
    assert set(changes) == {"deletion_requests", "deletion_blocks"}


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_trust_revision_offline_changes_only_provision_storage(
    tmp_path: Path, operation: str
) -> None:
    target = (
        "0007_verbatim_anchors:0008_provision_trust"
        if operation == "upgrade"
        else "0008_provision_trust:0007_verbatim_anchors"
    )
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
    changes = re.findall(r"(?:CREATE|DROP|ALTER) TABLE\s+(\w+)", result.stdout)
    assert changes
    assert set(changes) == {"provision_records"}


def test_upgrade_generates_four_evidence_tables(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "0002_evidence_ledger",
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


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_delivery_revision_offline_changes_only_delivery_storage(
    tmp_path: Path, operation: str
) -> None:
    target = (
        "0002_evidence_ledger:0003_delivery_scope"
        if operation == "upgrade"
        else "0003_delivery_scope:0002_evidence_ledger"
    )
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
    changes = re.findall(r"(?:CREATE|DROP|ALTER) TABLE\s+(\w+)", result.stdout)
    assert changes == ["delivery_scopes"]


def test_provision_revision_upgrades_from_bank_projection_offline(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "0005_bank_projection:0006_provision_records",
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
    changes = re.findall(r"(?:CREATE|DROP|ALTER) TABLE\s+(\w+)", result.stdout)
    assert changes == ["provision_records"]


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_anchor_revision_offline_changes_only_anchor_storage(
    tmp_path: Path, operation: str
) -> None:
    target = (
        "0006_provision_records:0007_verbatim_anchors"
        if operation == "upgrade"
        else "0007_verbatim_anchors:0006_provision_records"
    )
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
    changes = re.findall(r"(?:CREATE|DROP|ALTER) TABLE\s+(\w+)", result.stdout)
    assert changes == ["verbatim_anchors"]
    extension = (
        r"CREATE EXTENSION (?:IF NOT EXISTS )?pgroonga"
        if operation == "upgrade"
        else r"DROP EXTENSION pgroonga"
    )
    assert re.search(extension, result.stdout, re.IGNORECASE)
