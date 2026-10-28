import importlib.util
import json
import math
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Literal, Protocol, cast

import pytest
from pydantic import BaseModel

pytestmark = pytest.mark.ut
STUB_PATH = Path(__file__).resolve().parents[1] / "support" / "openai_stub.py"


class Stub(Protocol):
    Handler: type[BaseHTTPRequestHandler]

    def chat_completion(self, request: dict[str, object]) -> dict[str, object]: ...

    def embed(self, text: str) -> list[float]: ...

    def embeddings(self, request: dict[str, object]) -> dict[str, object]: ...


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Stub:
    spec = importlib.util.spec_from_file_location("openai_stub", STUB_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return cast(Stub, module)


def completion_content(stub: Stub, request: dict[str, object]) -> str:
    response = stub.chat_completion(request)
    assert response["object"] == "chat.completion"
    choices = cast(list[dict[str, object]], response["choices"])
    assert choices[0]["finish_reason"] == "stop"
    message = cast(dict[str, object], choices[0]["message"])
    assert message["role"] == "assistant"
    content = message["content"]
    assert isinstance(content, str)
    return content


def test_startup_probe_answers_ok(stub: Stub) -> None:
    request: dict[str, object] = {
        "model": "stub",
        "messages": [{"role": "user", "content": "Say 'ok'"}],
    }
    assert completion_content(stub, request) == "ok"


def test_explicit_schema_overrides_longer_prompt_schema(stub: Stub) -> None:
    explicit = {
        "type": "object",
        "properties": {"selected": {"const": "explicit"}},
        "required": ["selected"],
    }
    prompt = {
        "type": "object",
        "properties": {"selected": {"const": "prompt"}, "additional": {"type": "string"}},
        "required": ["selected", "additional"],
    }
    request: dict[str, object] = {
        "messages": [{"role": "user", "content": json.dumps(prompt)}],
        "response_format": {"type": "json_schema", "json_schema": {"schema": explicit}},
    }
    assert json.loads(completion_content(stub, request)) == {"selected": "explicit"}


@pytest.mark.parametrize(
    "fences", [("", ""), ("```json\n", "\n```"), ("~~~json\n", "\n~~~"), ("```json\n", "")]
)
def test_prompt_uses_outermost_longest_schema_not_properties_text(
    stub: Stub, fences: tuple[str, str]
) -> None:
    outer = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"const": "x"}},
                "required": ["inner"],
            }
        },
        "required": ["outer"],
    }
    shorter = {"properties": {"wrong": {"const": "shorter"}}, "required": ["wrong"]}
    decoy = {"description": "properties " * 100}
    text = (
        f'{json.dumps(shorter)} properties {{"description":"properties"}} '
        f"{json.dumps(decoy)} {fences[0]}{json.dumps(outer)}{fences[1]} {{broken"
    )
    request: dict[str, object] = {
        "messages": [{"role": "user", "content": text}],
        "response_format": {"type": "json_object"},
    }
    assert json.loads(completion_content(stub, request)) == {"outer": {"inner": "x"}}


class Fact(BaseModel):
    what: str
    when: str
    where: str
    who: str
    why: str
    fact_type: Literal["world", "experience"]


class Extraction(BaseModel):
    facts: list[Fact]


@pytest.mark.parametrize("definitions", ["$defs", "definitions"])
def test_extraction_preserves_content_lines_and_required_fact_fields(
    stub: Stub, definitions: str
) -> None:
    schema = Extraction.model_json_schema()
    if definitions == "definitions":
        schema[definitions] = schema.pop("$defs")
        schema = json.loads(json.dumps(schema).replace("#/$defs/", "#/definitions/"))
    lines = ["架空の花子はお茶が好きです。", "架空の太郎は東京タワーに行きました。"]
    request: dict[str, object] = {
        "messages": [
            {"role": "system", "content": json.dumps(schema)},
            {"role": "user", "content": "Extract facts.\nContent:\n" + "\n\n".join(lines)},
            {"role": "user", "content": "Content:\nこの後続メッセージは使わない。"},
        ],
        "response_format": {"type": "json_object"},
    }
    result = Extraction.model_validate_json(completion_content(stub, request))
    assert [fact.what for fact in result.facts] == lines


def test_consolidation_returns_deterministic_schema_conforming_content(stub: Stub) -> None:
    schema = {
        "type": "object",
        "properties": {
            "observations": {"type": "array", "items": {"type": "string"}},
            "kind": {"const": "consolidation"},
        },
        "required": ["observations", "kind"],
    }
    request: dict[str, object] = {
        "messages": [
            {
                "role": "system",
                "content": "You are a memory consolidation system\n" + json.dumps(schema),
            },
            {"role": "user", "content": "Consolidate the supplied synthetic facts."},
        ],
        "response_format": {"type": "json_object"},
    }
    first = json.loads(completion_content(stub, request))
    second = json.loads(completion_content(stub, request))
    assert first == second
    assert first["kind"] == "consolidation"
    assert isinstance(first["observations"], list)
    assert all(isinstance(item, str) for item in first["observations"])


@pytest.mark.parametrize("text", ["", "茶", "架空の花子はお茶が好きです。"])
def test_embedding_is_finite_normalized_and_384_dimensional(stub: Stub, text: str) -> None:
    vector = stub.embed(text)
    assert len(vector) == 384
    assert all(math.isfinite(value) for value in vector)
    assert math.sqrt(sum(value * value for value in vector)) == pytest.approx(1.0)
    assert stub.embed(text) == vector


def test_similar_japanese_texts_have_higher_embedding_similarity(stub: Stub) -> None:
    original = stub.embed("架空の花子は毎朝温かいお茶を飲みます。")
    similar = stub.embed("架空の花子は毎朝冷たいお茶を飲みます。")
    unrelated = stub.embed("銀河の探査船が未知の惑星に着陸した。")
    assert sum(a * b for a, b in zip(original, similar, strict=True)) > sum(
        a * b for a, b in zip(original, unrelated, strict=True)
    )


def test_embedding_is_stable_across_python_hash_seeds(tmp_path: Path) -> None:
    program = (
        "import json, runpy, sys; "
        "stub = runpy.run_path(sys.argv[1]); "
        "print(json.dumps(stub['embed']('架空の花子はお茶が好きです。')))"
    )
    outputs = []
    for seed in ("1", "2"):
        result = subprocess.run(
            [sys.executable, "-c", program, str(STUB_PATH)],
            env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONHASHSEED": seed},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(json.loads(result.stdout))
    assert len(outputs[0]) == 384
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("inputs", ["お茶", ["お茶", "架空の猫"]])
def test_embedding_response_preserves_input_order(stub: Stub, inputs: str | list[str]) -> None:
    response = stub.embeddings({"input": inputs})
    data = cast(list[dict[str, object]], response["data"])
    texts = [inputs] if isinstance(inputs, str) else inputs
    assert response["object"] == "list"
    assert [item["index"] for item in data] == list(range(len(texts)))
    assert [item["embedding"] for item in data] == [stub.embed(text) for text in texts]


def test_schema_response_handles_nullable_reference_and_scalar_fields(stub: Stub) -> None:
    schema = {
        "$defs": {"value": {"type": "integer"}},
        "properties": {
            "number": {"$ref": "#/$defs/value"},
            "flag": {"type": "boolean"},
            "nullable": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        },
        "required": ["number", "flag", "nullable"],
    }
    content = completion_content(
        stub,
        {
            "messages": [{"role": "user", "content": "Respond using the supplied schema."}],
            "response_format": {"json_schema": {"schema": schema}},
        },
    )
    output = json.loads(content)
    assert isinstance(output["number"], int) and not isinstance(output["number"], bool)
    assert isinstance(output["flag"], bool)
    assert output["nullable"] is None or isinstance(output["nullable"], str)


def test_server_initializes_request_log_without_starting_on_import(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handlers = []

    def initialize(
        self: ThreadingHTTPServer,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
    ) -> None:
        handlers.append(handler)

    monkeypatch.setattr(ThreadingHTTPServer, "__init__", initialize)
    log = tmp_path / "requests" / "requests.jsonl"
    server = cast(ModuleType, stub).StubServer(("127.0.0.1", 0), log)
    assert handlers == [stub.Handler]
    assert server.request_log == log and log.read_text() == ""


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("GET", "/v1/models", b"", 200),
        ("POST", "/v1/chat/completions", b'{"messages":[{"role":"user","content":"Say ok"}]}', 200),
        ("POST", "/v1/embeddings", '{"input":["お茶","架空の猫"]}'.encode(), 200),
        ("POST", "/v1/embeddings", b'{"input":[1]}', 400),
        ("POST", "/v1/chat/completions", b'{"messages":null}', 400),
        ("POST", "/v1/chat/completions", b"{broken", 400),
        ("POST", "/unknown", b"{}", 404),
        ("GET", "/unknown", b"", 404),
    ],
)
def test_http_handler_returns_protocol_response_and_records_received_request(
    stub: Stub, tmp_path: Path, method: str, path: str, body: bytes, status: int
) -> None:
    request = f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    output = bytearray()

    class Connection:
        def makefile(self, mode: str, buffering: int) -> BytesIO:
            return BytesIO(request)

        def sendall(self, data: bytes) -> None:
            output.extend(data)

    log = tmp_path / "requests.jsonl"
    server = SimpleNamespace(request_log=log, log_lock=threading.Lock())
    stub.Handler(
        cast("socket.socket", Connection()), ("127.0.0.1", 12345), cast(HTTPServer, server)
    )
    headers, response = bytes(output).split(b"\r\n\r\n", 1)
    assert int(headers.splitlines()[0].split()[1]) == status
    decoded = json.loads(response)
    if status == 200:
        assert "error" not in decoded
    else:
        assert "error" in decoded
    if (
        method == "POST"
        and path in ("/v1/chat/completions", "/v1/embeddings")
        and body != b"{broken"
    ):
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert records == [{"path": path, "req": json.loads(body)}]
