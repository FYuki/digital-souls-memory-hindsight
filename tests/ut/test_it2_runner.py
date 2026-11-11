"""Run the shell entry point against isolated command doubles, never a GPU."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]
MODEL_HASHES = {
    "gemma4-12b.gguf": "1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606",
    "bge-m3-q8_0.gguf": "aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173",
    "bge-reranker-v2-m3-Q8_0.gguf": (
        "a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3"
    ),
}


@pytest.fixture
def runner(tmp_path: Path) -> dict[str, str]:
    script = ROOT / "scripts" / "run-it2.sh"
    assert script.is_file(), "IT2 runner has not been implemented"
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copyfile(script, project / "scripts" / script.name)
    for filename in ("compose.it2.yml", "compose.it2.gpt-6-luna.yml"):
        source = ROOT / filename
        assert source.is_file(), f"missing IT2 configuration: {filename}"
        shutil.copyfile(source, project / filename)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, signal, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['DOCKER_CALLS'], 'a') as f: f.write(json.dumps(args)+'\\n')\n"
        "command = next((x for x in args if x in "
        "('build','pull','up','run','down','logs','ps','inspect',"
        "'stop','rm','kill','prune')), '')\n"
        "if command == os.environ.get('SIGNAL_COMMAND'): os.kill(os.getppid(), signal.SIGTERM)\n"
        "if command == 'up':\n"
        "    pathlib.Path(os.environ['STARTED_FLAG']).touch()\n"
        "    pathlib.Path(os.environ['SELECTED_GPU_LOG']).write_text("
        "os.environ['IT2_GPU_DEVICE'])\n"
        "if command == 'ps':\n"
        "    rows = [{'ID':'synthetic-'+name,'Service':name,'State':'running','ExitCode':0} "
        "for name in ('llama-generation','llama-embedding','llama-reranker')]\n"
        "    if os.environ.get('OOM'): rows[0].update(State='exited', ExitCode=137)\n"
        "    if '--status' in args and args[args.index('--status')+1] == 'exited':\n"
        "        rows = [row for row in rows if row['State'] == 'exited']\n"
        "    print(json.dumps(rows) if '--format' in args else "
        "'\\n'.join(row['ID'] for row in rows))\n"
        "if command == 'logs':\n"
        "    print('CUDA out of memory' if os.environ.get('OOM') else 'synthetic llama log')\n"
        "if command == 'run':\n"
        "    pathlib.Path(os.environ['COMPOSE_ENV_LOG']).write_text(json.dumps({\n"
        "        key: os.environ.get(key) for key in ('IT2_GENERATION_MODEL','IT2_ARTIFACT_DIR')\n"
        "    }))\n"
        "if command == 'run' and os.environ.get('OOM'):\n"
        "    import time\n"
        "    time.sleep(2)\n"
        "sys.exit(7 if command == os.environ.get('FAILED_COMMAND') else 0)\n"
    )
    docker.chmod(0o755)
    gpu = binaries / "nvidia-smi"
    gpu.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "started = pathlib.Path(os.environ['STARTED_FLAG']).exists()\n"
        "with open(os.environ['GPU_CALLS'], 'a') as f:\n"
        "    f.write(json.dumps({'started':started, 'args':sys.argv[1:]})+'\\n')\n"
        "query = next((arg.split('=',1)[1] for arg in sys.argv "
        "if arg.startswith('--query-gpu=')), None)\n"
        "fields = {'memory.free':os.environ.get('GPU_FREE','24000'), "
        "'memory.used':'1234','memory.total':'24576','name':'synthetic GPU','index':'0'}\n"
        "if query == 'index,memory.free' and 'GPU_ROWS' in os.environ:\n"
        "    print(os.environ['GPU_ROWS'])\n"
        "else:\n"
        "    print(', '.join(fields[field] for field in query.split(',')) "
        "if query else 'synthetic GPU 1234 MiB / 24576 MiB')\n"
        "sys.exit(int(os.environ.get('GPU_EXIT','0')))\n"
    )
    gpu.chmod(0o755)
    models = tmp_path / "models"
    models.mkdir()
    for filename in MODEL_HASHES:
        (models / filename).write_text("synthetic, deliberately not a model")
    return {
        "PATH": str(binaries) + os.pathsep + os.defpath,
        "HOME": str(tmp_path),
        "DOCKER_CALLS": str(tmp_path / "calls.jsonl"),
        "GPU_CALLS": str(tmp_path / "gpu.jsonl"),
        "STARTED_FLAG": str(tmp_path / "started"),
        "SELECTED_GPU_LOG": str(tmp_path / "selected-gpu"),
        "COMPOSE_ENV_LOG": str(tmp_path / "compose-environment.json"),
        "IT2_MODEL_DIR": str(models),
        "IT2_ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "IT2_LOCK_FILE": str(tmp_path / "it2.lock"),
        "TEST_SCRIPT": str(project / "scripts" / script.name),
    }


def _run(runner: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    result = subprocess.run(
        ["bash", runner["TEST_SCRIPT"]],
        env=runner,
        cwd=runner["HOME"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    calls_path = Path(runner["DOCKER_CALLS"])
    calls = (
        [json.loads(line) for line in calls_path.read_text().splitlines()]
        if calls_path.exists()
        else []
    )
    return result, calls


def _accept_model_hashes(runner: dict[str, str]) -> None:
    """Only lifecycle tests substitute hashing; mismatch tests use real sha256sum."""
    fake = Path(runner["PATH"].split(os.pathsep)[0]) / "sha256sum"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        f"hashes = {MODEL_HASHES!r}\n"
        "if '-c' in sys.argv or '--check' in sys.argv: sys.exit(0)\n"
        "for path in sys.argv[1:]:\n"
        "    if not path.startswith('-'):\n"
        "        name = pathlib.Path(path).name\n"
        "        digest = '0'*64 if os.environ.get('CORRUPT_MODEL') == name else hashes[name]\n"
        "        print(digest + '  ' + path)\n"
    )
    fake.chmod(0o755)


@pytest.mark.parametrize("missing", [False, True])
def test_hash_mismatch_or_missing_model_prevents_any_docker_call(
    runner: dict[str, str], missing: bool
) -> None:
    if missing:
        (Path(runner["IT2_MODEL_DIR"]) / "gemma4-12b.gguf").unlink()
    result, calls = _run(runner)
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize("model", ["unknown", "", "GEMMA4-12B"])
def test_unknown_generation_model_does_not_fall_back(runner: dict[str, str], model: str) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"IT2_GENERATION_MODEL": model})
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize(
    ("free", "exit_status"), [("10999", "0"), ("not-a-number", "0"), ("24000", "1")]
)
def test_insufficient_or_unreadable_vram_prevents_startup(
    runner: dict[str, str], free: str, exit_status: str
) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"GPU_FREE": free, "GPU_EXIT": exit_status})
    assert result.returncode != 0
    assert not any("up" in call or "run" in call or "build" in call for call in calls)


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("missing", "Persistent credentials directory is required"),
        ("repo", "Credentials directory must be outside the repository"),
        ("artifacts", "Credentials directory must be outside the artifacts"),
        ("temporary", "Credentials directory must be persistent"),
    ],
)
def test_luna_rejects_unsafe_auth_location_before_docker(
    runner: dict[str, str], location: str, message: str
) -> None:
    from tempfile import TemporaryDirectory

    _accept_model_hashes(runner)
    root = Path(runner["HOME"])
    # Each rule has its own message, so a case passes only through its own rule. The
    # temporary case is placed under /tmp itself so it does not depend on TMPDIR.
    with TemporaryDirectory(prefix="it2-auth-test-", dir="/tmp") as temporary:
        directories = {
            "missing": root / "missing",
            "repo": root / "project" / "auth",
            "artifacts": Path(runner["IT2_ARTIFACT_DIR"]) / "auth",
            "temporary": Path(temporary) / "auth",
        }
        auth = directories[location]
        if location != "missing":
            auth.mkdir(parents=True)
            (auth / "auth.json").write_text('{"synthetic": true}', encoding="utf-8")
        result, calls = _run(
            runner | {"IT2_GENERATION_MODEL": "gpt-6-luna", "IT2_CODEX_HOME": str(auth)}
        )
    assert result.returncode != 0
    assert result.stderr.strip().splitlines()[-1] == message
    assert calls == []


@pytest.mark.parametrize("failed_command", ["", "build", "up", "run"])
def test_runner_preserves_status_and_cleans_only_fixed_project(
    runner: dict[str, str], failed_command: str
) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"FAILED_COMMAND": failed_command})
    assert result.returncode == (7 if failed_command else 0), result.stderr
    assert calls
    assert "down" in calls[-1] and "--volumes" in calls[-1]
    for call in calls:
        assert call[0] == "compose"
        assert "--project-name" in call
        assert call[call.index("--project-name") + 1] == "dsm-hindsight-it2"
        assert not set(call) & {"stop", "rm", "kill", "prune"}


def test_term_signal_still_cleans_fixed_project(runner: dict[str, str]) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"SIGNAL_COMMAND": "up"})
    assert result.returncode == 143
    assert "down" in calls[-1]


def test_llama_oom_fails_even_when_test_command_would_succeed(runner: dict[str, str]) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"OOM": "1"})
    assert result.returncode != 0
    assert any("up" in call for call in calls)
    assert "down" in calls[-1]


@pytest.mark.parametrize("free", ["11000", "24000"])
def test_runner_records_gpu_usage_after_startup(runner: dict[str, str], free: str) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"GPU_FREE": free})
    assert result.returncode == 0, result.stderr
    assert any("up" in call for call in calls)
    artifacts = Path(runner["IT2_ARTIFACT_DIR"])
    assert any("1234" in path.read_text() for path in artifacts.rglob("*") if path.is_file())
    gpu_calls = [json.loads(line) for line in Path(runner["GPU_CALLS"]).read_text().splitlines()]
    assert any(call["started"] for call in gpu_calls)


def test_runner_waits_for_lock_before_docker_then_continues_after_release(
    runner: dict[str, str],
) -> None:
    _accept_model_hashes(runner)
    with Path(runner["IT2_LOCK_FILE"]).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = subprocess.Popen(
            ["bash", runner["TEST_SCRIPT"]],
            env=runner,
            cwd=runner["HOME"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                child.communicate(timeout=0.5)
            calls_path = Path(runner["DOCKER_CALLS"])
            assert not calls_path.exists() or calls_path.read_text() == ""
            fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stdout + stderr
            assert calls_path.read_text()
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=5)


@pytest.mark.parametrize("filename", list(MODEL_HASHES))
def test_each_required_model_hash_is_checked_before_docker(
    runner: dict[str, str],
    filename: str,
) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"CORRUPT_MODEL": filename})
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize(
    ("rows", "expected"),
    [("0, 6000\n1, 6000", None), ("0, 6000\n1, 12000", "1")],
)
def test_vram_is_not_summed_and_selected_gpu_reaches_compose(
    runner: dict[str, str],
    rows: str,
    expected: str | None,
) -> None:
    _accept_model_hashes(runner)
    result, calls = _run(runner | {"GPU_ROWS": rows})
    if expected is None:
        assert result.returncode != 0
        assert not any("up" in call for call in calls)
    else:
        assert result.returncode == 0, result.stderr
        assert Path(runner["SELECTED_GPU_LOG"]).read_text() == expected


def test_luna_uses_overlay_with_writable_auth_outside_subject_project(
    runner: dict[str, str],
) -> None:
    from tempfile import TemporaryDirectory

    _accept_model_hashes(runner)
    # The subject is the copied project in tmp_path, not this checkout.
    with TemporaryDirectory(prefix=".it2-auth-test-", dir=ROOT) as directory:
        auth = Path(directory)
        (auth / "auth.json").write_text('{"synthetic": true}')
        result, calls = _run(
            runner
            | {
                "IT2_GENERATION_MODEL": "gpt-6-luna",
                "IT2_CODEX_HOME": str(auth),
            }
        )
    assert result.returncode == 0, result.stderr
    assert all(
        any(value.endswith("compose.it2.gpt-6-luna.yml") for value in call) for call in calls
    )


@pytest.mark.parametrize("use_defaults", [False, True])
def test_runner_exports_resolved_model_and_artifact_directory_to_compose(
    runner: dict[str, str],
    use_defaults: bool,
) -> None:
    _accept_model_hashes(runner)
    environment = dict(runner)
    if use_defaults:
        environment.pop("IT2_ARTIFACT_DIR")
    else:
        environment["IT2_GENERATION_MODEL"] = "gemma4-12b"
    result, _ = _run(environment)
    assert result.returncode == 0, result.stderr
    exported = json.loads(Path(runner["COMPOSE_ENV_LOG"]).read_text())
    assert exported["IT2_GENERATION_MODEL"] == "gemma4-12b"
    assert isinstance(exported["IT2_ARTIFACT_DIR"], str)
    artifacts = Path(exported["IT2_ARTIFACT_DIR"])
    assert artifacts.is_absolute() and artifacts.is_dir()
    if use_defaults:
        assert artifacts.parent == Path(runner["TEST_SCRIPT"]).parents[1] / ".it2-artifacts"
    else:
        assert artifacts == Path(runner["IT2_ARTIFACT_DIR"])
