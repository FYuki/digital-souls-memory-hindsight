"""One chat-completions adapter for identity, consistency and volatile state."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypedDict, cast

import httpx

from . import openai_prompts
from .character_state.domain import StateInput
from .consistency.ports import ConsistencyReview, ReviewFailed, checked_review
from .identity.ports import IdentityUnavailable, Temperament


class _ClientOptions(TypedDict):
    timeout: float
    transport: httpx.MockTransport | None
    trust_env: bool


class _RequestOptions(TypedDict):
    url: str
    headers: dict[str, str]
    json: dict[str, object]


class _GatewayUnavailable(Exception):
    """Transport or HTTP status failed, with no upstream diagnostics retained."""


class _InvalidCompletion(Exception):
    """A completed HTTP request did not contain a usable JSON completion."""


def _decode_completion(response: httpx.Response) -> object:
    if not response.is_success:
        raise _GatewayUnavailable()
    try:
        envelope = response.json()
        if not isinstance(envelope, dict):
            raise _InvalidCompletion()
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _InvalidCompletion()
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise _InvalidCompletion()
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _InvalidCompletion()
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise _InvalidCompletion()
        result: object = json.loads(content)
        return result
    except (ValueError, UnicodeError):
        raise _InvalidCompletion() from None


@dataclass(frozen=True)
class OpenAICompatibleGateway:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float
    transport: httpx.MockTransport | None = field(default=None, repr=False, kw_only=True)

    def _body(self, prompt: str, data: Mapping[str, object]) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(dict(data), ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }

    def _client_options(self) -> _ClientOptions:
        return {
            "timeout": self.timeout_seconds,
            "transport": self.transport,
            "trust_env": False,
        }

    def _request_options(self, prompt: str, data: Mapping[str, object]) -> _RequestOptions:
        return {
            "url": self.base_url.rstrip("/") + "/chat/completions",
            "headers": {"Authorization": "Bearer " + self.api_key},
            "json": self._body(prompt, data),
        }

    def _complete(self, prompt: str, data: Mapping[str, object]) -> object:
        try:
            with httpx.Client(**self._client_options()) as client:
                response = client.post(**self._request_options(prompt, data))
        except httpx.HTTPError:
            raise _GatewayUnavailable() from None
        return _decode_completion(response)

    def _generate_text(self, prompt: str, data: Mapping[str, object], key: str) -> str:
        try:
            raw = self._complete(prompt, data)
        except (_GatewayUnavailable, _InvalidCompletion):
            raise IdentityUnavailable() from None
        value = raw.get(key) if isinstance(raw, Mapping) else None
        if not isinstance(value, str) or not value.strip():
            raise IdentityUnavailable()
        return value

    def _review(self, prompt: str, data: Mapping[str, object]) -> ConsistencyReview:
        try:
            raw = self._complete(prompt, data)
        except (_GatewayUnavailable, _InvalidCompletion):
            raise ReviewFailed() from None
        if not isinstance(raw, Mapping):
            raise ReviewFailed()
        # checked_review owns strict validation; casts do not coerce upstream values.
        return checked_review(
            ConsistencyReview(cast(bool, raw.get("consistent")), cast(str, raw.get("reason")))
        )

    def render_personality(
        self, aspects: dict[str, float], temperament: Temperament, renderer_version: str
    ) -> str:
        return self._generate_text(
            openai_prompts.RENDER_PERSONALITY,
            {
                "aspects": aspects,
                "temperament": temperament.description,
                "renderer_version": renderer_version,
            },
            "description",
        )

    def respond_in_voice(
        self, situation: str, answer: str, question: str | None, temperament: str | None
    ) -> str:
        return self._generate_text(
            openai_prompts.RESPOND_IN_VOICE,
            {
                "situation": situation,
                "answer": answer,
                "question": question,
                "temperament": temperament,
            },
            "response",
        )

    def review_personality(self, temperament: str, personality: str) -> ConsistencyReview:
        return self._review(
            openai_prompts.REVIEW_PERSONALITY,
            {"temperament": temperament, "personality": personality},
        )

    def review_voice(
        self,
        situation: str,
        response: str,
        answer: str,
        question: str | None,
        temperament: str | None,
    ) -> ConsistencyReview:
        return self._review(
            openai_prompts.REVIEW_VOICE,
            {
                "situation": situation,
                "response": response,
                "answer": answer,
                "question": question,
                "temperament": temperament,
            },
        )

    async def generate_state(
        self, character_id: str, session_id: str, surface: str, turns: tuple[StateInput, ...]
    ) -> object:
        data: dict[str, object] = {
            "character_id": character_id,
            "session_id": session_id,
            "surface": surface,
            "turns": [
                {
                    "turn_id": turn.turn_id,
                    "actor": turn.actor,
                    "kind": turn.kind,
                    "text": turn.text,
                    "occurred_at": turn.occurred_at.isoformat(),
                }
                for turn in turns
            ],
        }
        try:
            async with httpx.AsyncClient(**self._client_options()) as client:
                response = await client.post(
                    **self._request_options(openai_prompts.GENERATE_STATE, data)
                )
        except httpx.HTTPError:
            raise _GatewayUnavailable() from None
        try:
            return _decode_completion(response)
        except _InvalidCompletion:
            # A non-Mapping lets the existing generator terminate as invalid_output.
            return None
