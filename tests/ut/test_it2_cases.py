"""Validate distributed synthetic cases and reject incomplete case contracts."""

import shutil
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.ut
CASES = Path(__file__).resolve().parents[1] / "it2" / "cases"


@pytest.fixture
def contract() -> ModuleType:
    return import_module("tests.it2.cases_contract")


def test_shipped_cases_load_with_unique_ids_and_validate(contract: ModuleType) -> None:
    cases = contract.load_cases(CASES)
    contract.validate_cases(cases)
    assert cases
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


def test_duplicate_ids_across_case_files_are_rejected(contract: ModuleType, tmp_path: Path) -> None:
    import json

    cases = contract.load_cases(CASES)
    for path in CASES.glob("*.json"):
        shutil.copyfile(path, tmp_path / path.name)
    first = next(case for case in cases if case["acceptance_id"] == "AC-M1")
    second = next(case for case in cases if case["acceptance_id"] == "AC-R3")
    (tmp_path / "same_fact.json").write_text(json.dumps([first]))
    (tmp_path / "japanese.json").write_text(json.dumps([{**second, "id": first["id"]}]))
    with pytest.raises(ValueError, match=first["id"]):
        contract.load_cases(tmp_path)


@pytest.mark.parametrize("field", ["id", "input", "expected", "scoring_unit"])
def test_required_case_fields_cannot_be_omitted(contract: ModuleType, field: str) -> None:
    cases = deepcopy(contract.load_cases(CASES))
    del cases[0][field]
    with pytest.raises(ValueError):
        contract.validate_cases(cases)


@pytest.mark.parametrize("difference", ["number", "negation", "subject", "tense"])
def test_all_four_fact_differences_are_required(contract: ModuleType, difference: str) -> None:
    cases = contract.load_cases(CASES)
    incomplete = [
        case
        for case in cases
        if not (case["acceptance_id"] == "AC-M2" and case["difference"] == difference)
    ]
    with pytest.raises(ValueError):
        contract.validate_cases(incomplete)


@pytest.mark.parametrize("unit", ["extracted_fact", "returned_source"])
def test_japanese_extraction_and_source_are_independently_required(
    contract: ModuleType, unit: str
) -> None:
    cases = contract.load_cases(CASES)
    incomplete = [
        case
        for case in cases
        if not (case["acceptance_id"] == "AC-R3" and case["scoring_unit"] == unit)
    ]
    with pytest.raises(ValueError):
        contract.validate_cases(incomplete)


@pytest.mark.parametrize("split", ["tuning", "final"])
@pytest.mark.parametrize("positive", [False, True])
def test_abstention_requires_positive_and_negative_queries_in_both_splits(
    contract: ModuleType, split: str, positive: bool
) -> None:
    cases = contract.load_cases(CASES)
    incomplete = [
        case
        for case in cases
        if not (
            case["acceptance_id"] == "AC-R4"
            and case["split"] == split
            and case["positive"] is positive
        )
    ]
    with pytest.raises(ValueError):
        contract.validate_cases(incomplete)


def test_tuning_queries_cannot_be_reused_in_final_confirmation(contract: ModuleType) -> None:
    cases = deepcopy(contract.load_cases(CASES))
    tuning = next(case for case in cases if case.get("split") == "tuning")
    final = next(case for case in cases if case.get("split") == "final")
    final["input"]["query"] = tuning["input"]["query"]
    with pytest.raises(ValueError):
        contract.validate_cases(cases)


def test_same_fact_requires_all_mention_references(
    contract: ModuleType,
) -> None:
    cases = deepcopy(contract.load_cases(CASES))
    same = next(case for case in cases if case["acceptance_id"] == "AC-M1")
    same["expected"]["source_mention_ids"] = []
    with pytest.raises(ValueError):
        contract.validate_cases(cases)


def test_same_fact_requires_multiple_sessions_even_with_all_mentions(
    contract: ModuleType,
) -> None:
    cases = deepcopy(contract.load_cases(CASES))
    same = next(case for case in cases if case["acceptance_id"] == "AC-M1")
    sessions = same["input"]["sessions"]
    same["input"]["sessions"] = [
        {
            **sessions[0],
            "mentions": [mention for session in sessions for mention in session["mentions"]],
        }
    ]
    with pytest.raises(ValueError):
        contract.validate_cases(cases)


@pytest.mark.parametrize("feature", ["proper_name", "date", "verbatim"])
def test_japanese_cases_require_each_preservation_feature(
    contract: ModuleType, feature: str
) -> None:
    cases = contract.load_cases(CASES)
    incomplete = [
        case
        for case in cases
        if not (case["acceptance_id"] == "AC-R3" and case["feature"] == feature)
    ]
    with pytest.raises(ValueError):
        contract.validate_cases(incomplete)


def test_only_final_cases_are_required_for_acceptance(contract: ModuleType) -> None:
    cases = contract.load_cases(CASES)
    required = contract.required_case_ids(cases)
    for case in cases:
        if case.get("split") == "tuning":
            assert case["id"] not in required
        else:
            assert case["id"] in required


def test_manifest_cannot_change_fixed_trial_count(contract: ModuleType, tmp_path: Path) -> None:
    import json

    for path in CASES.glob("*.json"):
        shutil.copyfile(path, tmp_path / path.name)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["trial_count"] = 1
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        contract.load_cases(tmp_path)


@pytest.mark.parametrize("feature", ["proper_name", "date", "verbatim"])
@pytest.mark.parametrize("unit", ["extracted_fact", "returned_source"])
def test_each_japanese_feature_requires_both_evaluation_units(
    contract: ModuleType,
    feature: str,
    unit: str,
) -> None:
    cases = contract.load_cases(CASES)
    incomplete = [
        case
        for case in cases
        if not (
            case["acceptance_id"] == "AC-R3"
            and case["feature"] == feature
            and case["scoring_unit"] == unit
        )
    ]
    with pytest.raises(ValueError):
        contract.validate_cases(incomplete)
