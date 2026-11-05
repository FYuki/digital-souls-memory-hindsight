"""Chat completions at the HTTP boundary, with real port consumers."""

import asyncio
import importlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import timedelta
from typing import Protocol, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest

from digital_souls_memory_hindsight.character_state.domain import CharacterState, StateInput
from digital_souls_memory_hindsight.character_state.generator import CharacterStateGenerator
from digital_souls_memory_hindsight.character_state.ports import ContextGateway, StateJob
from digital_souls_memory_hindsight.consistency.ports import (
    ConsistencyReview,
    Reviewer,
    ReviewFailed,
    checked_review,
)
from digital_souls_memory_hindsight.evidence.domain import Session, StateKey
from digital_souls_memory_hindsight.identity.ports import (
    IdentityUnavailable,
    LLMGateway,
    Temperament,
)
from tests.ut.test_character_state_domain import source
from tests.ut.test_delivery_scope import NOW, Clock

pytestmark = pytest.mark.ut
SECRET = "synthetic-secret-llm-token"
STATE = {"mood": "穏やか", "relationship_temperature": "温かい", "current_topic": "お茶"}
OPERATIONS = (
    "render_personality",
    "respond_in_voice",
    "review_personality",
    "review_voice",
    "generate_state",
)


class GatewayPorts(LLMGateway, Reviewer, ContextGateway, Protocol):
    pass


def gateway(handler: Callable[[httpx.Request], httpx.Response]) -> GatewayPorts:
    module = importlib.import_module("digital_souls_memory_hindsight.openai_compatible")
    return cast(
        GatewayPorts,
        module.OpenAICompatibleGateway(
            base_url="http://synthetic-llm:8080/v1",
            model="synthetic-model",
            api_key=SECRET,
            timeout_seconds=60,
            transport=httpx.MockTransport(handler),
        ),
    )


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "synthetic-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "synthetic-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def invoke(port: GatewayPorts, operation: str) -> object:
    if operation == "render_personality":
        return port.render_personality(
            {"Openness": 0.71329}, Temperament("char-a", 2, "架空の気質", NOW), "renderer-1"
        )
    if operation == "respond_in_voice":
        return port.respond_in_voice("架空の場面", "架空の回答", "架空の質問", "架空の気質")
    if operation == "review_personality":
        return port.review_personality("架空の気質", "架空の人格")
    if operation == "review_voice":
        return port.review_voice(
            "架空の場面", "架空の試作発話", "架空の回答", "架空の質問", "架空の気質"
        )
    assert operation == "generate_state"
    return asyncio.run(
        port.generate_state(
            "char-a", "s-1", "Web", (StateInput("t-1", "char-a", "utterance", "架空のお茶", NOW),)
        )
    )


@pytest.mark.parametrize("operation", OPERATIONS)
def test_all_ports_send_configured_request_and_versioned_prompt(operation: str) -> None:
    requests: list[httpx.Request] = []
    content: object
    if operation == "render_personality":
        content = {"description": "架空の人格"}
    elif operation == "respond_in_voice":
        content = {"response": "架空の試作発話"}
    elif operation == "generate_state":
        content = STATE
    else:
        content = {
            "consistent": False,
            "reason": "  架空の不一致理由  ",
            "aspects": {"Openness": 1},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completion(json.dumps(content, ensure_ascii=False))

    result = invoke(gateway(handler), operation)
    if operation.startswith("review_"):
        assert asdict(checked_review(result)) == {
            "consistent": False,
            "reason": "  架空の不一致理由  ",
        }
    elif operation == "generate_state":
        assert result == STATE
    else:
        assert result == ("架空の人格" if operation == "render_personality" else "架空の試作発話")
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://synthetic-llm:8080/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer " + SECRET
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 60)
    body = json.loads(request.content)
    assert body["model"] == "synthetic-model"
    assert body["response_format"] == {"type": "json_object"}
    system, user = body["messages"]
    assert system["role"] == "system"
    assert re.match(r"\[dsm-memory/[a-z_-]+/v[0-9]+\]", system["content"])
    prompts = importlib.import_module("digital_souls_memory_hindsight.openai_prompts")
    assert system["content"] in vars(prompts).values()
    assert user["role"] == "user"
    data = json.loads(user["content"])
    serialized = json.dumps(data, ensure_ascii=False)
    if operation == "render_personality":
        assert data["aspects"] == {"Openness": 0.71329}
        assert "架空の気質" in serialized
        assert "renderer-1" in serialized
    elif operation == "respond_in_voice":
        for expected in ("架空の場面", "架空の回答", "架空の質問", "架空の気質"):
            assert expected in serialized
    elif operation == "generate_state":
        for expected in ("char-a", "s-1", "Web", "t-1", "架空のお茶"):
            assert expected in serialized
    elif operation.startswith("review_"):
        for expected in (
            ("架空の気質", "架空の人格")
            if operation == "review_personality"
            else ("架空の場面", "架空の試作発話", "架空の回答", "架空の質問", "架空の気質")
        ):
            assert expected in serialized
        for forbidden in ("aspects", "openness", "0.71329"):
            assert forbidden not in serialized.casefold()


@pytest.mark.parametrize("operation", OPERATIONS[:4])
@pytest.mark.parametrize(
    "body", ["{broken", "{}", '{"choices": []}', '{"choices": [{"message": {"content": null}}]}']
)
def test_invalid_completion_envelope_is_rejected(operation: str, body: str) -> None:
    with pytest.raises(ReviewFailed if operation.startswith("review_") else IdentityUnavailable):
        invoke(gateway(lambda request: httpx.Response(200, content=body)), operation)


@pytest.mark.parametrize("operation", OPERATIONS[:4])
@pytest.mark.parametrize("content", ["not json", "null", "{}", '""', "", " \t "])
def test_invalid_content_cannot_generate_or_approve(operation: str, content: str) -> None:
    with pytest.raises(ReviewFailed if operation.startswith("review_") else IdentityUnavailable):
        invoke(gateway(lambda request: completion(content)), operation)


@pytest.mark.parametrize("operation", ["render_personality", "respond_in_voice"])
@pytest.mark.parametrize("value", [None, 7, "", " \t "])
def test_generated_text_must_be_nonblank(operation: str, value: object) -> None:
    field = "description" if operation == "render_personality" else "response"
    with pytest.raises(IdentityUnavailable):
        invoke(gateway(lambda request: completion(json.dumps({field: value}))), operation)


@pytest.mark.parametrize("operation", OPERATIONS[:4])
@pytest.mark.parametrize("finish_reason", ["length", "tool_calls", None])
def test_incomplete_completion_cannot_generate_or_approve(
    operation: str, finish_reason: str | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = completion(
            json.dumps(
                {
                    "description": "架空の人格",
                    "response": "架空の発話",
                    "consistent": True,
                    "reason": "架空の審査理由",
                }
            )
        )
        body = response.json()
        body["choices"][0]["finish_reason"] = finish_reason
        return httpx.Response(200, json=body)

    with pytest.raises(ReviewFailed if operation.startswith("review_") else IdentityUnavailable):
        invoke(gateway(handler), operation)


@pytest.mark.parametrize("operation", ["review_personality", "review_voice"])
@pytest.mark.parametrize(
    "review",
    [
        {"consistent": "true", "reason": "理由"},
        {"consistent": 1, "reason": "理由"},
        {"consistent": True},
        {"reason": "理由"},
        {"consistent": True, "reason": ""},
        {"consistent": True, "reason": " \t "},
        {"consistent": True, "reason": 1},
    ],
)
def test_review_requires_strict_boolean_and_nonblank_reason(operation: str, review: object) -> None:
    with pytest.raises(ReviewFailed):
        invoke(gateway(lambda request: completion(json.dumps(review))), operation)


def test_personality_review_does_not_reuse_seed_aspects() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return completion(
            json.dumps(
                {"description": "架空の人格"}
                if len(requests) == 1
                else {"consistent": True, "reason": "架空の審査理由"}
            )
        )

    port = gateway(handler)
    invoke(port, "render_personality")
    assert invoke(port, "review_personality") == ConsistencyReview(True, "架空の審査理由")
    messages = cast(list[dict[str, str]], requests[1]["messages"])
    user_data = json.loads(messages[1]["content"])
    serialized = json.dumps(user_data, ensure_ascii=False).casefold()
    assert "架空の人格" in serialized
    for forbidden in ("aspects", "openness", "0.71329"):
        assert forbidden not in serialized


@pytest.mark.parametrize("operation", OPERATIONS[:4])
@pytest.mark.parametrize("failure", ["timeout", "connection", "401", "429", "500"])
def test_transport_failure_is_classified_without_secret_in_diagnostics(
    operation: str, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "connection":
            raise httpx.ConnectError(SECRET, request=request)
        return httpx.Response(int(failure), text=SECRET)

    port = gateway(handler)
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(
            ReviewFailed if operation.startswith("review_") else IdentityUnavailable
        ) as error,
    ):
        invoke(port, operation)
    for diagnostic in (repr(port), str(error.value), repr(error.value), caplog.text):
        assert SECRET.casefold() not in diagnostic.casefold()


@pytest.mark.parametrize(
    "failure",
    ["timeout", "500", "envelope", "json", "missing", "blank", "long", "incomplete", "valid"],
)
def test_state_generator_classifies_real_adapter_result_and_preserves_deadline(
    failure: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "500":
            return httpx.Response(500)
        if failure == "envelope":
            return httpx.Response(200, json={})
        if failure == "json":
            return completion("broken")
        if failure == "incomplete":
            response = completion(json.dumps(STATE))
            body = response.json()
            body["choices"][0]["finish_reason"] = "length"
            return httpx.Response(200, json=body)
        value = dict(STATE)
        if failure == "missing":
            del value["mood"]
        if failure in {"blank", "long"}:
            value["mood"] = " \t " if failure == "blank" else "あ" * 201
        return completion(json.dumps(value))

    job = StateJob(
        StateKey("char-a", "s-1", "Web"), 1, 1, "synthetic-lease", NOW + timedelta(seconds=60)
    )
    store, evidence, deletion = MagicMock(), MagicMock(), MagicMock()
    store.claim.return_value = job
    evidence.get_session.return_value = Session("s-1", NOW, None, "Web", True)
    evidence.state_input_sources.return_value = (source("t-1", 0, "架空のお茶"),)
    deletion.turns_under_deletion.return_value = frozenset()
    generator = CharacterStateGenerator(store, evidence, deletion, gateway(handler), clock=Clock())
    with patch(
        "digital_souls_memory_hindsight.character_state.generator.asyncio.wait_for",
        wraps=asyncio.wait_for,
    ) as wait:
        asyncio.run(generator.run_once())
    assert wait.call_args.kwargs["timeout"] == 30
    if failure == "valid":
        store.save.assert_called_once_with(job, CharacterState(**STATE), NOW)
        store.fail.assert_not_called()
        store.defer.assert_not_called()
    else:
        store.save.assert_not_called()
        if failure in {"timeout", "500"}:
            store.defer.assert_called_once_with(
                job, NOW, NOW + timedelta(seconds=1), "gateway_unavailable"
            )
            store.fail.assert_not_called()
        else:
            store.fail.assert_called_once_with(job, NOW, "invalid_output")
            store.defer.assert_not_called()
