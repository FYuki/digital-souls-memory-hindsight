"""Machine-readable synthetic corpus validation before any model is run."""

import json
from collections import Counter
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from pydantic import TypeAdapter

from .scoring import SCORER_VERSION


class Mention(TypedDict):
    id: str
    text: str


class Session(TypedDict):
    session_id: str
    mentions: list[Mention]


class CaseInput(TypedDict):
    sessions: list[Session]
    query: NotRequired[str]


class Expected(TypedDict, total=False):
    source_mention_ids: list[str]
    consolidated_fact_count: int
    distinct_fact_count: int
    facts: list[str]
    preserve: list[str]
    empty: bool


class Case(TypedDict):
    id: str
    acceptance_id: Literal["AC-M1", "AC-M2", "AC-R3", "AC-R4"]
    input: CaseInput
    expected: Expected
    scoring_unit: str
    difference: NotRequired[str]
    feature: NotRequired[str]
    split: NotRequired[Literal["tuning", "final"]]
    positive: NotRequired[bool]


class Manifest(TypedDict):
    schema_version: int
    scorer_version: str
    trial_count: int
    case_files: list[str]
    character_ids: list[str]


_CASES: TypeAdapter[list[Case]] = TypeAdapter(list[Case])
_MANIFEST: TypeAdapter[Manifest] = TypeAdapter(Manifest)
CASE_FILES = ("same_fact.json", "difference_fact.json", "japanese.json", "abstention.json")


def load_cases(directory: Path) -> list[Case]:
    manifest = _MANIFEST.validate_python(
        json.loads((directory / "manifest.json").read_text(encoding="utf-8")), strict=True
    )
    expected_characters = {
        f"it2-{Path(filename).stem.replace('_', '-')}-t{trial}"
        for filename in CASE_FILES
        for trial in range(1, 4)
    } | {"it2-performance"}
    if (
        manifest["trial_count"] != 3
        or manifest["schema_version"] != 1
        or manifest["scorer_version"] != SCORER_VERSION
        or manifest["case_files"] != list(CASE_FILES)
        or set(manifest["character_ids"]) != expected_characters
        or len(manifest["character_ids"]) != len(expected_characters)
    ):
        raise ValueError("Manifest must fix three independent trials for every suite")
    cases: list[Case] = []
    for filename in CASE_FILES:
        path = directory / filename
        cases.extend(
            _CASES.validate_python(json.loads(path.read_text(encoding="utf-8")), strict=True)
        )
    _unique_ids(cases)
    return cases


def _unique_ids(cases: list[Case]) -> None:
    duplicates = sorted(
        identifier
        for identifier, count in Counter(case["id"] for case in cases).items()
        if count > 1
    )
    if duplicates:
        raise ValueError("Duplicate case ID: " + ", ".join(duplicates))


def validate_cases(cases: list[Case]) -> None:
    checked = _CASES.validate_python(cases, strict=True)
    _unique_ids(checked)
    if not checked:
        raise ValueError("Required case corpus is empty")
    for case in checked:
        sessions = case["input"]["sessions"]
        mentions = [mention for session in sessions for mention in session["mentions"]]
        if (
            not case["id"]
            or not case["scoring_unit"]
            or not sessions
            or not mentions
            or not case["expected"]
        ):
            raise ValueError("Case lacks input, expected result or scoring unit")
        if any(not session["session_id"] for session in sessions) or any(
            not mention["id"] or not mention["text"] for mention in mentions
        ):
            raise ValueError("Synthetic sessions and mentions require identifiers and text")
        if case["acceptance_id"] == "AC-M1":
            ids = [mention["id"] for mention in mentions]
            if len({session["session_id"] for session in sessions}) < 2 or (
                len(ids) != len(set(ids))
                or set(case["expected"].get("source_mention_ids", [])) != set(ids)
                or case["expected"].get("consolidated_fact_count") != 1
            ):
                raise ValueError("Same Fact requires multiple sessions and all mention references")
        elif case["acceptance_id"] == "AC-M2":
            if len(mentions) < 2 or case["expected"].get("distinct_fact_count") != 2:
                raise ValueError("Different Facts require a distinct pair")
        elif case["acceptance_id"] == "AC-R3":
            if not case["expected"].get("preserve"):
                raise ValueError("Japanese case lacks explicit preservation targets")
        else:
            if not case["input"].get("query") or "positive" not in case or "split" not in case:
                raise ValueError("Abstention case lacks query, polarity or split")
            if case["expected"].get("empty") is not (not case["positive"]):
                raise ValueError("Abstention expected result disagrees with query polarity")
    if not any(case["acceptance_id"] == "AC-M1" for case in checked):
        raise ValueError("Same Fact case is missing")
    differences = {case.get("difference") for case in checked if case["acceptance_id"] == "AC-M2"}
    if not {"number", "negation", "subject", "tense"} <= differences:
        raise ValueError("All four Fact differences are required")
    japanese = [case for case in checked if case["acceptance_id"] == "AC-R3"]
    required_japanese = {
        (feature, unit)
        for feature in ("proper_name", "date", "verbatim")
        for unit in ("extracted_fact", "returned_source")
    }
    if not required_japanese <= {(case.get("feature"), case["scoring_unit"]) for case in japanese}:
        raise ValueError("Every Japanese feature requires extraction and returned source scoring")
    abstention = [case for case in checked if case["acceptance_id"] == "AC-R4"]
    if {(case["split"], case["positive"]) for case in abstention} != {
        ("tuning", True),
        ("tuning", False),
        ("final", True),
        ("final", False),
    }:
        raise ValueError("Both query polarities are required in tuning and final")
    queries = [case["input"]["query"] for case in abstention]
    if len(queries) != len(set(queries)):
        raise ValueError("Abstention queries must not overlap")


def required_case_ids(cases: list[Case]) -> frozenset[str]:
    validate_cases(cases)
    return frozenset(case["id"] for case in cases if case.get("split") != "tuning")
