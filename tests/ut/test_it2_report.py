"""Public report CLI: allowlisted data, safe identifiers and atomic rejection."""

import json
import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "20261105T010203Z-gemma4-12b"


@pytest.fixture(autouse=True)
def report_module_exists() -> None:
    # A missing CLI must not make rejection cases pass for an unrelated reason.
    import_module("tests.it2.report")


def _payload() -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "authorization": "synthetic inputs only; supervisor execution",
        "environment": {"gpu": "synthetic GPU"},
        "revision": "ec6171b2e626ba6e4102cc9e813f1890415dd655",
        "timestamp": "2026-11-05T01:02:03Z",
        "models": {"generation": "gemma4-12b", "sha256": "1" * 64},
        "images": {
            "llama": "sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7"
        },
        "settings": {"recall_min_reranker_score": 0.5},
        "scorer_version": "2",
        "completion": "complete",
        "trials": [{"bank_id": f"b{i}", "passed": True} for i in range(1, 4)],
        "costs_and_constraints": "synthetic fixture; no model executed",
        "synthetic_inputs": ["星見町で十一月五日に会う。"],
    }


def _generate(tmp_path: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload, ensure_ascii=False))
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.it2.report",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "reports"),
        ],
        cwd=ROOT,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_report_cli_creates_named_report_and_preserves_allowed_values(tmp_path: Path) -> None:
    payload = _payload()
    result = _generate(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "reports" / f"{RUN_ID}.json").read_text()) == payload


def test_report_accepts_digest_and_git_revision_without_secret_false_positive(
    tmp_path: Path,
) -> None:
    payload = _payload()
    result = _generate(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    saved = json.loads((tmp_path / "reports" / f"{RUN_ID}.json").read_text())
    assert saved["images"] == payload["images"]
    assert saved["revision"] == payload["revision"]


@pytest.mark.parametrize("identifier", ["../escape", "/escape", "nested/report", ""])
def test_report_rejects_unsafe_identifier_without_creating_files(
    tmp_path: Path, identifier: str
) -> None:
    result = _generate(tmp_path, {**_payload(), "run_id": identifier})
    assert result.returncode != 0
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.json")) == [
        "input.json"
    ]


def test_report_refuses_to_overwrite_existing_run(tmp_path: Path) -> None:
    output = tmp_path / "reports" / f"{RUN_ID}.json"
    output.parent.mkdir()
    output.write_text('{"original": true}')
    result = _generate(tmp_path, _payload())
    assert result.returncode != 0
    assert output.read_text() == '{"original": true}'


@pytest.mark.parametrize("key", ["docker_inspect", "environment_variables", "raw_logs", "auth"])
def test_report_rejects_unapproved_fields(tmp_path: Path, key: str) -> None:
    result = _generate(tmp_path, {**_payload(), key: "unapproved synthetic value"})
    assert result.returncode != 0
    assert not list((tmp_path / "reports").glob("*.json"))


@pytest.mark.parametrize(
    "unsafe",
    [
        "sk-" + "syntheticOnly" * 5,
        "ghp_" + "a" * 36,
        "github_pat_" + "syntheticOnly" * 5,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGlj",
        "Bearer synthetic-credential",
        "/home/synthetic-user/.codex/auth.json",
        "/Users/synthetic-user/models",
        "/root/.codex/auth.json",
        "C:\\Users\\synthetic-user\\auth.json",
        "合成sk-" + "syntheticOnly" * 5,
        "sk-a",
        "Bearer\nsynthetic-credential",
    ],
)
def test_report_rejects_nested_secrets_without_echo_or_partial_output(
    tmp_path: Path, unsafe: str
) -> None:
    result = _generate(tmp_path, {**_payload(), "synthetic_inputs": [{"text": unsafe}]})
    assert result.returncode != 0
    assert unsafe not in result.stdout + result.stderr
    assert not list((tmp_path / "reports").glob("*.json"))


def test_report_rejects_wrong_scorer_version(tmp_path: Path) -> None:
    result = _generate(tmp_path, {**_payload(), "scorer_version": "unknown-version"})
    assert result.returncode != 0
    assert not list((tmp_path / "reports").glob("*.json"))
