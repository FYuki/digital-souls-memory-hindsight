"""Deterministic OpenAI fixture for isolated IT1, using only the standard library."""

import hashlib
import json
import math
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from uuid import uuid4

DIMENSIONS = 384
CHAT_PATH = "/v1/chat/completions"
EMBEDDINGS_PATH = "/v1/embeddings"
CONSOLIDATION_PROMPT = "You are a memory consolidation system"


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array")
    return cast(list[object], value)


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    return [_object(item) for item in _array(request["messages"])]


def _schema_in_prompt(messages: list[dict[str, object]]) -> dict[str, object] | None:
    text = "\n".join(
        content for message in messages if isinstance(content := message.get("content"), str)
    )
    decoder = json.JSONDecoder()
    best: dict[str, object] | None = None
    longest = 0
    for start, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "properties" in candidate and end - start > longest:
            best = candidate
            longest = end - start
    return best


def _select_schema(
    request: dict[str, object], messages: list[dict[str, object]]
) -> dict[str, object] | None:
    response_format = request.get("response_format")
    if response_format is not None:
        explicit = _object(response_format).get("json_schema")
        if explicit is not None:
            return _object(_object(explicit)["schema"])
    return _schema_in_prompt(messages)


def _resolve(schema: dict[str, object], root: dict[str, object]) -> dict[str, object]:
    visited: set[str] = set()
    while "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ValueError("Only local JSON references are supported")
        if reference in visited:
            raise ValueError("Cyclic JSON reference")
        visited.add(reference)
        target = root
        for part in reference[2:].split("/"):
            target = _object(target[part.replace("~1", "/").replace("~0", "~")])
        schema = target
    return schema


def _minimal(schema: dict[str, object], root: dict[str, object]) -> object:
    schema = _resolve(schema, root)
    for union in ("anyOf", "oneOf"):
        if union in schema:
            alternatives = [_object(item) for item in _array(schema[union])]
            selected = next(
                (item for item in alternatives if _resolve(item, root).get("type") != "null"),
                alternatives[0],
            )
            return _minimal(selected, root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return _array(schema["enum"])[0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "null")
    if kind == "object" or "properties" in schema:
        properties = _object(schema.get("properties", {}))
        required = _array(schema.get("required", list(properties)))
        return {
            name: _minimal(_object(properties[name]), root)
            for name in required
            if isinstance(name, str)
        }
    if kind == "array":
        return []
    if kind == "string":
        return ""
    if kind in ("integer", "number"):
        return 0
    if kind == "boolean":
        return False
    if kind == "null" or not schema:
        return None
    raise ValueError("Unsupported JSON Schema")


def _extraction_response(
    schema: dict[str, object], messages: list[dict[str, object]]
) -> dict[str, object]:
    output = _object(_minimal(schema, schema))
    user = next((message for message in messages if message["role"] == "user"), None)
    content = user.get("content") if user is not None else None
    properties = _object(schema.get("properties", {}))
    if "facts" in properties and isinstance(content, str) and "Content:" in content:
        items = _object(_object(properties["facts"])["items"])
        facts = []
        for line in content.split("Content:", 1)[1].splitlines():
            if not line.strip():
                continue
            fact = _object(_minimal(items, schema))
            fact["what"] = line.strip()
            for field in ("when", "where", "who", "why"):
                if field in fact and isinstance(fact[field], str):
                    fact[field] = "N/A"
            facts.append(fact)
        output["facts"] = facts
    return output


def _consolidation_response(
    schema: dict[str, object], messages: list[dict[str, object]]
) -> dict[str, object]:
    output = _object(_minimal(schema, schema))
    if "creates" not in _object(schema.get("properties", {})):
        return output
    user = next(message for message in messages if message["role"] == "user")
    text = (
        str(user["content"]).split("### New facts", 1)[1].split("### Existing observations", 1)[0]
    )
    creates = []
    for identifier, fact in re.findall(r"^\[([0-9a-f-]{36})\] (.+)$", text, re.MULTILINE):
        body = re.split(r" \(occurred_start=| \(occurred_end=| \(mentioned_at=", fact, maxsplit=1)[
            0
        ]
        creates.append({"text": body, "source_fact_ids": [identifier], "reason": "fixture fact"})
    output["creates"] = creates
    output["updates"] = []
    output["deletes"] = []
    return output


def chat_completion(request: dict[str, object]) -> dict[str, object]:
    messages = _messages(request)
    schema = None if request.get("tools") else _select_schema(request, messages)
    content = "ok"
    if schema is not None:
        consolidation = any(
            isinstance(text := message.get("content"), str)
            and text.startswith(CONSOLIDATION_PROMPT)
            for message in messages
        )
        output = (
            _consolidation_response(schema, messages)
            if consolidation
            else _extraction_response(schema, messages)
        )
        content = json.dumps(output, ensure_ascii=False)
    message = (
        _reflect_message(request, messages)
        if request.get("tools")
        else {"role": "assistant", "content": content}
    )
    return {
        "id": "stub",
        "object": "chat.completion",
        "created": 0,
        "model": "stub",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                "message": message,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _reflect_message(
    request: dict[str, object], messages: list[dict[str, object]]
) -> dict[str, object]:
    available = {
        str(_object(_object(tool)["function"])["name"]) for tool in _array(request["tools"])
    }
    used = {
        str(_object(_object(call)["function"])["name"])
        for message in messages
        for call in _array(message.get("tool_calls", []))
    }
    memories: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    for message in messages:
        if message["role"] == "tool":
            result = _object(json.loads(str(message["content"])))
            memories.extend(_object(value) for value in _array(result.get("memories", [])))
            observations.extend(_object(value) for value in _array(result.get("observations", [])))
    choice = request.get("tool_choice")
    if isinstance(choice, dict):
        name = str(_object(choice["function"])["name"])
    else:
        name = next(
            (
                value
                for value in (
                    "search_mental_models",
                    "search_observations",
                    "recall",
                    "answer_ready",
                )
                if value in available and value not in used
            ),
            "done",
        )
    if name in {"search_mental_models", "search_observations", "recall"}:
        arguments: dict[str, object] = {
            "reason": "fixture retrieval",
            "query": "架空",
            "max_tokens": 10000,
        }
        if name == "search_mental_models":
            arguments.pop("max_tokens")
    elif name == "answer_ready":
        arguments = {"answer_found": bool(memories or observations)}
    elif name == "done":
        arguments = {
            "answer": "\n".join(str(value["text"]) for value in memories + observations)
            if memories or observations
            else "知っている事実はありません。",
            "answer_found": bool(memories or observations),
            "memory_ids": [str(value["id"]) for value in memories],
            "observation_ids": [str(value["id"]) for value in observations],
            "mental_model_ids": [],
        }
    else:
        raise ValueError("Unsupported fixture reflection tool")
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "fixture-" + str(len(messages)),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


def embed(text: str) -> list[float]:
    bounded = "\x02" + text + "\x03"
    vector = [0.0] * DIMENSIONS
    for left, right in zip(bounded, bounded[1:], strict=False):
        digest = hashlib.sha256((left + right).encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def embeddings(request: dict[str, object]) -> dict[str, object]:
    inputs = request["input"]
    texts = [inputs] if isinstance(inputs, str) else _array(inputs)
    if not all(isinstance(text, str) for text in texts):
        raise ValueError("Embedding input must contain strings")
    return {
        "object": "list",
        "model": "stub",
        "data": [
            {"object": "embedding", "index": index, "embedding": embed(cast(str, text))}
            for index, text in enumerate(texts)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@contextmanager
def hold_response(
    control_dir: Path | None, request: dict[str, object], *, timeout_seconds: float = 60
) -> Iterator[None]:
    text = json.dumps(request, ensure_ascii=False)
    gate = (
        next((path for path in control_dir.iterdir() if path.is_dir() and path.name in text), None)
        if control_dir is not None
        else None
    )
    if gate is None:
        yield
        return
    identifier = uuid4().hex
    for name in ("entered", "finished"):
        (gate / name).mkdir(exist_ok=True)
    (gate / "entered" / identifier).touch()
    deadline = time.monotonic() + timeout_seconds
    try:
        while not (gate / "release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Fixture response was not released")
            threading.Event().wait(0.01)
        yield
    finally:
        (gate / "finished" / identifier).touch()


class StubServer(ThreadingHTTPServer):
    def __init__(
        self, address: tuple[str, int], request_log: Path, control_dir: Path | None = None
    ) -> None:
        self.request_log = request_log
        self.log_lock = threading.Lock()
        self.control_dir = control_dir
        if control_dir is not None:
            control_dir.mkdir(parents=True, exist_ok=True)
        request_log.parent.mkdir(parents=True, exist_ok=True)
        request_log.touch()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: StubServer

    def _send(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "stub", "object": "model"}]})
        else:
            self._send(404, {"error": "Unknown endpoint"})

    def do_POST(self) -> None:
        if self.path not in (CHAT_PATH, EMBEDDINGS_PATH):
            self._send(404, {"error": "Unknown endpoint"})
            return
        try:
            request = _object(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            with self.server.log_lock, self.server.request_log.open("a", encoding="utf-8") as log:
                log.write(
                    json.dumps({"path": self.path, "req": request}, ensure_ascii=False) + "\n"
                )
            if self.path == CHAT_PATH:
                with hold_response(self.server.control_dir, request):
                    self._send(200, chat_completion(request))
            else:
                self._send(200, embeddings(request))
        except TimeoutError:
            self._send(503, {"error": "Fixture response was not released"})
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": "Invalid fixture request"})
            return


if __name__ == "__main__":
    control = os.environ.get("STUB_CONTROL_DIR")
    with StubServer(
        ("0.0.0.0", 8080),
        Path(os.environ["STUB_REQUEST_LOG"]),
        Path(control) if control is not None else None,
    ) as server:
        server.serve_forever()
