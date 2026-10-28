import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("failed_command", "signal_command", "expected"),
    [
        ("", "", 0),
        ("build", "", 7),
        ("up", "", 7),
        ("run", "", 7),
        ("down", "", 7),
        ("", "up", 143),
    ],
)
def test_runner_preserves_exit_status_and_collects_logs_before_cleanup(
    tmp_path: Path, failed_command: str, signal_command: str, expected: int
) -> None:
    fake = tmp_path / "docker"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['DOCKER_CALLS'], 'a') as log: log.write(json.dumps(args) + '\\n')\n"
        "commands = ('build','pull','up','logs','run','down')\n"
        "command = next(value for value in args if value in commands)\n"
        "if command == os.environ['SIGNAL_COMMAND']: os.kill(os.getppid(), signal.SIGTERM)\n"
        "if command == 'logs': print('Hindsight is running')\n"
        "sys.exit(7 if command == os.environ['FAILED_COMMAND'] else 0)\n"
    )
    fake.chmod(0o755)
    calls_path = tmp_path / "calls.jsonl"
    artifacts = tmp_path / "artifacts"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "run-it1.sh")],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path) + os.pathsep + os.defpath,
            "HOME": str(tmp_path),
            "DOCKER_CALLS": str(calls_path),
            "IT1_ARTIFACT_DIR": str(artifacts),
            "FAILED_COMMAND": failed_command,
            "SIGNAL_COMMAND": signal_command,
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert "logs" in calls[-2]
    assert "down" in calls[-1] and "--volumes" in calls[-1]
    assert "Hindsight is running" in (artifacts / "compose.log").read_text()
