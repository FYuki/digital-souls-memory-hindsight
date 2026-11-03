"""Synthetic port doubles shared by consistency and existing registration tests."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from digital_souls_memory_hindsight.consistency.ports import ConsistencyReview
    from digital_souls_memory_hindsight.identity.ports import Temperament


@dataclass(kw_only=True)
class Gateway:
    response: str = "架空の応答です。"
    generation_error: Exception | None = None
    voice_calls: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)

    def render_personality(
        self, aspects: dict[str, float], temperament: Temperament, renderer_version: str
    ) -> str:
        return "架空の人格の記述"

    def respond_in_voice(
        self, situation: str, answer: str, question: str | None, temperament: str | None
    ) -> str:
        self.voice_calls.append((situation, answer, question, temperament))
        if self.generation_error is not None:
            raise self.generation_error
        return self.response


@dataclass
class Reviewer:
    consistent: bool = True
    reason: str = "架空の審査理由"
    error: Exception | None = None
    invalid: str | None = None
    personality_calls: list[tuple[str, str]] = field(default_factory=list)
    voice_calls: list[tuple[str, str, str, str | None, str | None]] = field(default_factory=list)

    def result(self) -> ConsistencyReview:
        if self.error is not None:
            raise self.error
        module = importlib.import_module("digital_souls_memory_hindsight.consistency.ports")
        if self.invalid == "shape":
            return cast("ConsistencyReview", {"consistent": True, "reason": self.reason})
        return cast(
            "ConsistencyReview",
            module.ConsistencyReview(
                1 if self.invalid == "bool" else self.consistent,
                " \t " if self.invalid == "reason" else self.reason,
            ),
        )

    def review_personality(self, temperament: str, personality: str) -> ConsistencyReview:
        self.personality_calls.append((temperament, personality))
        return self.result()

    def review_voice(
        self,
        situation: str,
        response: str,
        answer: str,
        question: str | None,
        temperament: str | None,
    ) -> ConsistencyReview:
        self.voice_calls.append((situation, response, answer, question, temperament))
        return self.result()
