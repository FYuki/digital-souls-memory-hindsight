"""Fixed AC-I5 cases and deterministic scoring, independent of model execution."""

import json
import re
from collections.abc import Mapping, Sequence, Set
from pathlib import Path
from typing import TypedDict, cast

from digital_souls_memory_hindsight.identity_http import SeedRequest, TemperamentRequest
from digital_souls_memory_hindsight.voice_http import VoiceRequest

from .scoring import SCORER_VERSION


class ConsistencyCase(TypedDict):
    id: str
    level: str
    operation: str
    input: dict[str, object]
    expected: dict[str, object]


def consistency_character_id(*, case_id: str, trial: int) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", case_id) or trial not in (1, 2, 3):
        raise ValueError("Invalid consistency case or trial")
    return f"it2-consistency-{case_id}-t{trial}"


def load_consistency_cases(path: Path) -> list[ConsistencyCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("scorer_version") != SCORER_VERSION
        or payload.get("trial_count") != 3
        or not isinstance(payload.get("cases"), list)
    ):
        raise ValueError("Invalid consistency corpus")
    cases: list[ConsistencyCase] = []
    for raw in payload["cases"]:
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "level",
            "operation",
            "input",
            "expected",
        }:
            raise ValueError("Invalid consistency case fields")
        if (
            not isinstance(raw["id"], str)
            or not isinstance(raw["operation"], str)
            or not isinstance(raw["input"], dict)
            or not isinstance(raw["expected"], dict)
            or type(raw["expected"].get("consistent")) is not bool
        ):
            raise ValueError("Invalid consistency case values")
        consistency_character_id(case_id=raw["id"], trial=1)
        data = raw["input"]
        if raw["level"] == "reviewer":
            fields = (
                {"temperament", "personality"}
                if raw["operation"] == "personality"
                else {"situation", "response", "answer", "question", "temperament"}
            )
            if raw["operation"] not in {"personality", "voice"} or set(data) != fields:
                raise ValueError("Invalid reviewer input")
            for key, value in data.items():
                if (
                    key in {"question", "temperament"}
                    and value is None
                    and raw["operation"] == "voice"
                ):
                    continue
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Reviewer text must be nonblank")
        elif raw["level"] == "path":
            TemperamentRequest.model_validate({"description": data.get("temperament")})
            if raw["operation"] in {"seed", "temperament"}:
                SeedRequest.model_validate(data.get("seed"))
                if raw["operation"] == "temperament":
                    TemperamentRequest.model_validate({"description": data.get("update")})
            elif raw["operation"] in {"voice_first", "voice_no_rereview"}:
                VoiceRequest.model_validate(data.get("voice"))
            else:
                raise ValueError("Unknown path operation")
        else:
            raise ValueError("Unknown consistency level")
        cases.append(cast(ConsistencyCase, raw))
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate consistency case")
    coverage = {
        (case["level"], case["operation"], case["expected"]["consistent"]) for case in cases
    }
    required = {
        (level, operation, consistent)
        for level, operations in (
            ("reviewer", ("personality", "voice")),
            ("path", ("seed", "temperament", "voice_first")),
        )
        for operation in operations
        for consistent in (True, False)
    }
    if not required <= coverage or not any(c["operation"] == "voice_no_rereview" for c in cases):
        raise ValueError("Missing mandatory consistency cases")
    return cases


def _valid_review(value: object, consistent: object) -> bool:
    return (
        isinstance(value, Mapping)
        and type(value.get("consistent")) is bool
        and value["consistent"] == consistent
        and isinstance(value.get("reason"), str)
        and bool(value["reason"].strip())
    )


def score_step(
    *, expected: Mapping[str, object], observed: Mapping[str, object]
) -> dict[str, object]:
    outcome = "correct"
    reason = "all expected observations matched"
    generation = cast(int, observed["generation_calls"])
    review_calls = cast(int, observed["review_calls"])
    if generation > cast(int, expected["generation_calls"]) or review_calls > cast(
        int, expected["review_calls"]
    ):
        outcome, reason = "contract_violation", "unexpected component call"
    elif observed["generation_error"] is not None or (
        generation > 0
        and (
            not isinstance(observed["generated_text"], str)
            or not observed["generated_text"].strip()
        )
    ):
        outcome, reason = "generation_failed", "generation did not produce usable text"
    elif observed["review_error"] is not None:
        outcome, reason = "review_failed", "review did not complete"
    else:
        review = observed["review"]
        decision = expected["consistent"]
        if (
            decision is not None
            and isinstance(review, Mapping)
            and type(review.get("consistent")) is bool
            and review["consistent"] != decision
        ):
            outcome, reason = "misjudged", "completed review disagrees with expected decision"
        else:
            matches = (
                observed["status"] == expected["status"]
                and observed["saved"] == expected["saved"]
                and generation == expected["generation_calls"]
                and review_calls == expected["review_calls"]
                and observed["read_has_review"] is False
                and observed["response_review_present"] is True
                and (
                    review is None and observed["response_review"] is None
                    if decision is None
                    else _valid_review(review, decision)
                    and _valid_review(observed["response_review"], decision)
                )
            )
            if expected.get("current_unchanged") is True:
                matches = matches and observed["current_before"] == observed["current_after"]
            if "candidate_version_status" in expected:
                matches = (
                    matches
                    and observed["candidate_version_status"] == expected["candidate_version_status"]
                )
            if expected.get("review_row_unchanged") is True:
                matches = (
                    matches
                    and observed["review_row_before"] is not None
                    and observed["review_row_before"] == observed["review_row_after"]
                )
            for field in ("saved_version", "total_generation_calls", "total_review_calls"):
                if field in expected:
                    matches = matches and observed[field] == expected[field]
            if not matches:
                outcome, reason = (
                    "contract_violation",
                    "HTTP, storage, review or call count mismatch",
                )
    return {**observed, "outcome": outcome, "reason": reason}


def score_case(steps: Sequence[dict[str, object]]) -> dict[str, object]:
    if not steps:
        raise ValueError("A case must contain observations")
    primary = next((step for step in steps if step["outcome"] != "correct"), steps[0])
    return {"outcome": primary["outcome"], "reason": primary["reason"], "steps": list(steps)}


def summarize_consistency_trials(
    trials: Sequence[dict[str, object]], *, required_case_ids: Set[str]
) -> dict[str, object]:
    complete = len(trials) == 3 and {row["trial"] for row in trials} == {1, 2, 3}
    rows = [cast(list[dict[str, object]], row["cases"]) for row in trials]
    complete = (
        complete
        and bool(required_case_ids)
        and all(
            len({case["id"] for case in cases}) == len(cases)
            and {case["id"] for case in cases} == required_case_ids
            for cases in rows
        )
    )
    levels = {}
    for level in ("reviewer", "path"):
        selected = [case for cases in rows for case in cases if case["level"] == level]
        levels[level] = (
            "PASS" if selected and all(c["outcome"] == "correct" for c in selected) else "FAIL"
        )
    passed = complete and all(value == "PASS" for value in levels.values())
    return {"verdict": "PASS" if passed else "FAIL", "levels": levels, "trials": list(trials)}
