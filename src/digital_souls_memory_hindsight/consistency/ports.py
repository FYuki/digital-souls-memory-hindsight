"""Text-only review inputs and validated consistency results."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ConsistencyReview:
    consistent: bool
    reason: str


class ReviewFailed(Exception):
    """The reviewer did not return a usable decision."""


class Reviewer(Protocol):
    def review_personality(self, temperament: str, personality: str) -> ConsistencyReview: ...

    def review_voice(
        self,
        situation: str,
        response: str,
        answer: str,
        question: str | None,
        temperament: str | None,
    ) -> ConsistencyReview: ...


def checked_review(value: object) -> ConsistencyReview:
    if (
        not isinstance(value, ConsistencyReview)
        or not isinstance(value.consistent, bool)
        or not isinstance(value.reason, str)
        or not value.reason.strip()
    ):
        raise ReviewFailed()
    return value
