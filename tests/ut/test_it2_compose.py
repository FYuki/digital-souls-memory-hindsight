"""Render both real Compose configurations without starting containers."""

import json
import os
import subprocess
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
    read_only: bool


class Port(TypedDict):
    host_ip: str


class Service(TypedDict):
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


def _config(tmp_path: Path, *files: str) -> ComposeConfig:
    empty_env = tmp_path / "compose.env"
    empty_env.write_text("")
    args = ["docker", "compose", "--env-file", str(empty_env)]
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
