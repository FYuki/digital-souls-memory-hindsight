"""Render both real Compose configurations without starting containers."""

import json
import os
import subprocess
from importlib import import_module
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[2]
LLAMA_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp@"
    "sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7"
)


class Volume(TypedDict, total=False):
    type: str
    source: str
    target: str
    read_only: bool
    bind: dict[str, object]


class Port(TypedDict):
    host_ip: str


class Service(TypedDict):
    command: NotRequired[list[str]]
    image: NotRequired[str]
    build: NotRequired[dict[str, object]]
    read_only: NotRequired[bool]
    cap_drop: NotRequired[list[str]]
    security_opt: NotRequired[list[str]]
    networks: dict[str, object]
    volumes: NotRequired[list[Volume]]
    ports: NotRequired[list[Port]]
    environment: NotRequired[dict[str, str]]


class ComposeConfig(TypedDict):
    name: str
    services: dict[str, Service]
    networks: dict[str, dict[str, object]]


def _config(
    tmp_path: Path, *files: str, environment: dict[str, str] | None = None
) -> ComposeConfig:
    empty_env = tmp_path / "compose.env"
    empty_env.write_text("")
    args = ["docker", "compose", "--profile", "test", "--env-file", str(empty_env)]
    for filename in files:
        args.extend(["-f", str(ROOT / filename)])
    result = subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=ROOT,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "IT2_MODEL_DIR": str(tmp_path / "models"),
            "IT2_CODEX_HOME": str(tmp_path / "synthetic-auth"),
            "IT2_GPU_DEVICE": "1",
            **(environment if environment is not None else {}),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value: ComposeConfig = json.loads(result.stdout)
    return value


def test_it2_retains_it1_pins_and_hardens_three_llama_services(tmp_path: Path) -> None:
    baseline = _config(tmp_path, "compose.it1.yml")
    config = _config(tmp_path, "compose.it2.yml")
    assert config["name"] == "dsm-hindsight-it2"
    manifest = json.loads((ROOT / "tests/it2/cases/manifest.json").read_text())
    configured_characters = json.loads(
        config["services"]["service"]["environment"]["EVIDENCE_CHARACTER_IDS"]
    )
    assert configured_characters == manifest["character_ids"]
    for name in ("postgres", "hindsight", "service"):
        expected = baseline["services"][name]
        actual = config["services"][name]
        if "image" in expected:
            assert actual["image"] == expected["image"]
        if "build" in expected:
            assert actual["build"] == expected["build"]
    for name in ("llama-generation", "llama-embedding", "llama-reranker"):
        service = config["services"][name]
        assert service["image"] == LLAMA_IMAGE
        assert service["environment"]["CUDA_VISIBLE_DEVICES"] == "1"
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert any(
            item in ("no-new-privileges", "no-new-privileges:true")
            for item in service["security_opt"]
        )
        mounts = [volume for volume in service["volumes"] if volume["type"] == "bind"]
        assert mounts
        assert all(volume["read_only"] for volume in mounts)


@pytest.mark.parametrize("luna", [False, True])
def test_only_luna_hindsight_has_egress_and_persistent_writable_auth(
    tmp_path: Path, luna: bool
) -> None:
    files = ("compose.it2.yml", "compose.it2.gpt-6-luna.yml") if luna else ("compose.it2.yml",)
    config = _config(tmp_path, *files)
    for name, service in config["services"].items():
        external = [
            network
            for network in service["networks"]
            if not config["networks"][network].get("internal", False)
        ]
        assert bool(external) == (luna and name == "hindsight")
        assert all(port["host_ip"] == "127.0.0.1" for port in service.get("ports", []))
        auth = [
            volume
            for volume in service.get("volumes", [])
            if volume.get("source") == str(tmp_path / "synthetic-auth")
        ]
        assert bool(auth) == (luna and name == "hindsight")
        if auth:
            assert auth[0]["type"] == "bind"
            assert auth[0].get("read_only", False) is False
    hindsight = config["services"]["hindsight"]["environment"]
    assert hindsight["HINDSIGHT_API_LLM_PROVIDER"] == ("openai-codex" if luna else "openai")
    assert hindsight["HINDSIGHT_API_LLM_MODEL"] == ("gpt-6-luna" if luna else "gemma4-12b")
    if luna:
        assert not hindsight.get("HINDSIGHT_API_LLM_BASE_URL")
    assert config["services"]["llama-generation"]["image"] == LLAMA_IMAGE
    generation = config["services"]["service"]["environment"]
    assert generation["MEMORY_LLM_BASE_URL"] == "http://llama-generation:8080/v1"
    assert generation["MEMORY_LLM_MODEL"] == "gemma4-12b"
    assert generation["MEMORY_LLM_API_KEY"]
    assert float(generation["MEMORY_LLM_TIMEOUT_SECONDS"]) > 0


@pytest.mark.parametrize("model", ["gemma4-12b", "gpt-6-luna"])
def test_tests_receive_selected_model_and_persistent_writable_result_mount(
    tmp_path: Path,
    model: str,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    files = (
        ("compose.it2.yml", "compose.it2.gpt-6-luna.yml")
        if model == "gpt-6-luna"
        else ("compose.it2.yml",)
    )
    config = _config(
        tmp_path,
        *files,
        environment={
            "IT2_GENERATION_MODEL": model,
            "IT2_ARTIFACT_DIR": str(artifacts),
        },
    )
    service = config["services"]["tests"]
    assert service["environment"]["IT2_GENERATION_MODEL"] == model
    assert service["environment"]["IT2_RESULT_DIR"] == "/it2-artifacts"
    mounts = [
        volume for volume in service.get("volumes", []) if volume.get("target") == "/it2-artifacts"
    ]
    assert len(mounts) == 1
    assert mounts[0]["type"] == "bind"
    assert mounts[0]["source"] == str(artifacts)
    assert mounts[0].get("read_only", False) is False
    assert mounts[0].get("bind", {}).get("create_host_path", False) is False


def test_llama_tmpfs_is_one_mount_and_generation_disables_thinking() -> None:
    text = Path("compose.it2.yml").read_text(encoding="utf-8")
    assert 'tmpfs: ["/tmp:rw,noexec,nosuid,size=256m,mode=1777"]' in text
    assert '--reasoning, "off", --chat-template-kwargs, "{\\"enable_thinking\\":false}"' in text


@pytest.mark.parametrize("value", [None, "0.65"])
def test_threshold_is_identical_for_service_and_supervisor_tests(
    tmp_path: Path, value: str | None
) -> None:
    config = _config(
        tmp_path,
        "compose.it2.yml",
        environment={} if value is None else {"RECALL_MIN_RERANKER_SCORE": value},
    )
    environments = [config["services"][name]["environment"] for name in ("service", "tests")]
    assert [env["RECALL_MIN_RERANKER_SCORE"] for env in environments] == [value or "0.05"] * 2


def test_hindsight_reranker_candidates_are_capped(tmp_path: Path) -> None:
    config = _config(tmp_path, "compose.it2.yml")
    environment = config["services"]["hindsight"]["environment"]
    assert environment["HINDSIGHT_API_RERANKER_MAX_CANDIDATES"] == "100"


def test_performance_character_is_allowed(tmp_path: Path) -> None:
    config = _config(tmp_path, "compose.it2.yml")
    characters = json.loads(config["services"]["service"]["environment"]["EVIDENCE_CHARACTER_IDS"])
    assert "it2-performance" in characters


@pytest.mark.parametrize("luna", [False, True])
def test_e2e_characters_and_synthetic_author_are_shared_with_service(
    tmp_path: Path, luna: bool
) -> None:
    files = ("compose.it2.yml", "compose.it2.gpt-6-luna.yml") if luna else ("compose.it2.yml",)
    config = _config(tmp_path, *files)
    service = config["services"]["service"]["environment"]
    tests = config["services"]["tests"]["environment"]
    manifest = json.loads((ROOT / "tests/it2/cases/manifest.json").read_text())
    characters = json.loads(service["EVIDENCE_CHARACTER_IDS"])
    assert characters == manifest["character_ids"]
    assert {f"it2-e2e-t{trial}" for trial in (1, 2, 3)} <= set(characters)
    assert service["MEMORY_AUTHOR_ID"] == tests["MEMORY_AUTHOR_ID"] == "synthetic-author"
    assert service["MEMORY_AUTHOR_TOKEN"] == tests["MEMORY_AUTHOR_TOKEN"]
    assert service["MEMORY_AUTHOR_TOKEN"].startswith("synthetic-")


def test_generation_exposes_metrics(tmp_path: Path) -> None:
    config = _config(tmp_path, "compose.it2.yml")
    generation = config["services"]["llama-generation"]
    assert "--metrics" in generation["command"]


@pytest.mark.parametrize("luna", [False, True])
def test_consistency_tests_receive_same_component_model_configuration_as_service(
    tmp_path: Path, luna: bool
) -> None:
    files = ("compose.it2.yml", "compose.it2.gpt-6-luna.yml") if luna else ("compose.it2.yml",)
    config = _config(tmp_path, *files)
    service = config["services"]["service"]["environment"]
    tests = config["services"]["tests"]["environment"]
    for key in (
        "MEMORY_LLM_BASE_URL",
        "MEMORY_LLM_MODEL",
        "MEMORY_LLM_API_KEY",
        "MEMORY_LLM_TIMEOUT_SECONDS",
    ):
        assert tests[key] == service[key]


def test_recorded_performance_conditions_match_compose(tmp_path: Path) -> None:
    config = _config(tmp_path, "compose.it2.yml")
    performance_run = import_module("tests.it2.performance_run")
    conditions = performance_run.measurement_conditions("gemma4-12b")
    assert conditions["concurrency"] == 1
    assert conditions["query_length"] == len(
        performance_run.performance_query(index=0, positive=True)
    )
    for service, model in (
        ("llama-generation", "gemma4-12b"),
        ("llama-embedding", "bge-m3"),
        ("llama-reranker", "bge-reranker-v2-m3"),
    ):
        command = config["services"][service]["command"]
        assert conditions["model_context_lengths"][model] == int(
            command[command.index("--ctx-size") + 1]
        )
        assert conditions["model_gpu_layers"][model] == int(
            command[command.index("--gpu-layers") + 1]
        )
