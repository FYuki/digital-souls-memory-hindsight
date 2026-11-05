"""AC-I5 scoring contracts; no real model, database or network is used."""

import json
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

from tests.it2.scoring import SCORER_VERSION

pytestmark = pytest.mark.ut
CORPUS = Path(__file__).resolve().parents[1] / "it2/cases/consistency.json"


@pytest.fixture
def scoring() -> ModuleType:
    return import_module("tests.it2.consistency")


def expected_step(*, consistent: bool | None, status: int | None, saved: bool) -> dict[str, object]:
    return {
        "consistent": consistent,
        "status": status,
        "saved": saved,
        "generation_calls": 1,
        "review_calls": 1,
    }


def observed_step(*, consistent: bool | None, status: int | None, saved: bool) -> dict[str, object]:
    review = None if consistent is None else {"consistent": consistent, "reason": "合成の理由"}
    return {
        "status": status,
        "saved": saved,
        "review": review,
        "response_review": deepcopy(review),
        "response_review_present": True,
        "generated_text": "合成の生成文",
        "generation_calls": 1,
        "review_calls": 1,
        "generation_error": None,
        "review_error": None,
        "read_has_review": False,
    }


@pytest.mark.parametrize(
    ("consistent", "status", "saved"),
    [(True, 201, True), (False, 422, False), (False, 201, True)],
    ids=["identity-accepted", "identity-rejected", "voice-contradiction-accepted"],
)
def test_correct_requires_decision_http_and_storage(
    scoring: ModuleType, consistent: bool, status: int, saved: bool
) -> None:
    result = scoring.score_step(
        expected=expected_step(consistent=consistent, status=status, saved=saved),
        observed=observed_step(consistent=consistent, status=status, saved=saved),
    )
    assert result["outcome"] == "correct"


@pytest.mark.parametrize("consistent", [True, False])
def test_opposite_completed_review_is_misjudged(scoring: ModuleType, consistent: bool) -> None:
    result = scoring.score_step(
        expected=expected_step(consistent=consistent, status=201, saved=True),
        observed=observed_step(consistent=not consistent, status=422, saved=False),
    )
    assert result["outcome"] == "misjudged"


@pytest.mark.parametrize(
    ("changes", "outcome"),
    [
        ({"generation_error": "IdentityUnavailable", "status": 503}, "generation_failed"),
        ({"generated_text": " \t\n", "review_calls": 0, "status": 503}, "generation_failed"),
        ({"review_error": "ReviewFailed", "status": 503}, "review_failed"),
        (
            {"generation_error": "IdentityUnavailable", "review_error": "ReviewFailed"},
            "generation_failed",
        ),
        ({"generation_calls": 2, "generation_error": "IdentityUnavailable"}, "contract_violation"),
        ({"review_calls": 2, "review_error": "ReviewFailed"}, "contract_violation"),
    ],
)
def test_stage_failures_and_precedence_are_distinct(
    scoring: ModuleType, changes: dict[str, object], outcome: str
) -> None:
    observed = {**observed_step(consistent=True, status=201, saved=True), **changes}
    result = scoring.score_step(
        expected=expected_step(consistent=True, status=201, saved=True), observed=observed
    )
    assert result["outcome"] == outcome


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 200},
        {"saved": False},
        {"response_review": None},
        {"response_review_present": False},
        {"response_review": {"consistent": False, "reason": "別の理由"}},
        {"response_review": {"consistent": True, "reason": " \t\n"}},
        {"response_review": {"consistent": True}},
        {
            "generation_calls": 0,
            "review_calls": 0,
            "generated_text": None,
            "review": None,
            "response_review": None,
        },
        {
            "status": 404,
            "saved": False,
            "generation_calls": 0,
            "review_calls": 0,
            "generated_text": None,
            "review": None,
            "response_review": None,
        },
        {"review_calls": 0},
        {"read_has_review": True},
    ],
)
def test_success_status_alone_cannot_hide_contract_failure(
    scoring: ModuleType, changes: dict[str, object]
) -> None:
    result = scoring.score_step(
        expected=expected_step(consistent=True, status=201, saved=True),
        observed={**observed_step(consistent=True, status=201, saved=True), **changes},
    )
    assert result["outcome"] == "contract_violation"


def test_rejected_identity_must_not_be_saved(scoring: ModuleType) -> None:
    result = scoring.score_step(
        expected=expected_step(consistent=False, status=422, saved=False),
        observed=observed_step(consistent=False, status=422, saved=True),
    )
    assert result["outcome"] == "contract_violation"


@pytest.mark.parametrize("violation", [None, "current_changed", "candidate_exists"])
def test_rejected_temperament_preserves_current_and_has_no_new_version(
    scoring: ModuleType, violation: str | None
) -> None:
    before = {"temperament": {"version": 1}, "personality_profile": {"version": 1}}
    observed = {
        **observed_step(consistent=False, status=422, saved=False),
        "generation_calls": 0,
        "generated_text": None,
        "current_before": before,
        "current_after": {**before, "temperament": {"version": 2}}
        if violation == "current_changed"
        else deepcopy(before),
        "candidate_version_status": 200 if violation == "candidate_exists" else 404,
    }
    expected = {
        **expected_step(consistent=False, status=422, saved=False),
        "generation_calls": 0,
        "current_unchanged": True,
        "candidate_version_status": 404,
    }
    result = scoring.score_step(expected=expected, observed=observed)
    assert result["outcome"] == ("correct" if violation is None else "contract_violation")


@pytest.mark.parametrize("violation", [None, "calls", "review_row"])
def test_voice_lifecycle_scoring_requires_no_calls_and_unchanged_initial_review(
    scoring: ModuleType, violation: str | None
) -> None:
    row = {"character_id": "same-character", "consistent": False, "reason": "最初の審査"}
    first = scoring.score_step(
        expected=expected_step(consistent=False, status=201, saved=True),
        observed=observed_step(consistent=False, status=201, saved=True),
    )
    steps = [first]
    for stage in ("subsequent", "after_deletion", "after_restart"):
        observed = {
            **observed_step(consistent=None, status=201, saved=True),
            "generation_calls": 1 if violation == "calls" and stage == "after_restart" else 0,
            "review_calls": 1 if violation == "calls" and stage == "after_restart" else 0,
            "generated_text": None,
            "review_row_before": row,
            "review_row_after": {**row, "reason": "再審査の結果"}
            if violation == "review_row" and stage == "after_deletion"
            else deepcopy(row),
        }
        expected = {
            **expected_step(consistent=None, status=201, saved=True),
            "generation_calls": 0,
            "review_calls": 0,
            "review_row_unchanged": True,
        }
        steps.append(scoring.score_step(expected=expected, observed=observed))
    result = scoring.score_case(steps)
    assert result["outcome"] == ("correct" if violation is None else "contract_violation")
    assert len(result["steps"]) == 4


@pytest.mark.parametrize("reason", ["理由の表現は自由", "判定の根拠を別の言い方で説明する。"])
def test_reason_and_generated_text_are_not_compared_verbatim(
    scoring: ModuleType, reason: str
) -> None:
    observed = observed_step(consistent=True, status=201, saved=True)
    observed.update(
        generated_text="元の合成文とは異なる言い回し",
        review={"consistent": True, "reason": reason},
        response_review={"consistent": True, "reason": "HTTP側でも理由の存在だけを見る"},
    )
    result = scoring.score_step(
        expected=expected_step(consistent=True, status=201, saved=True), observed=observed
    )
    assert result["outcome"] == "correct"


def test_profileless_temperament_requires_null_review_and_no_component_calls(
    scoring: ModuleType,
) -> None:
    expected = {
        **expected_step(consistent=None, status=201, saved=True),
        "generation_calls": 0,
        "review_calls": 0,
    }
    observed = {
        **observed_step(consistent=None, status=201, saved=True),
        "generation_calls": 0,
        "review_calls": 0,
        "generated_text": None,
    }
    assert scoring.score_step(expected=expected, observed=observed)["outcome"] == "correct"
    observed["review_calls"] = 1
    assert (
        scoring.score_step(expected=expected, observed=observed)["outcome"] == "contract_violation"
    )


def test_case_result_uses_first_failure_and_preserves_all_steps(scoring: ModuleType) -> None:
    steps = [
        {"outcome": "correct", "reason": "準備完了"},
        {"outcome": "generation_failed", "reason": "generation_failed:IdentityUnavailable"},
        {"outcome": "review_failed", "reason": "review_failed:ReviewFailed"},
    ]
    result = scoring.score_case(steps)
    assert result["outcome"] == "generation_failed"
    assert result["reason"] == steps[1]["reason"]
    assert result["steps"] == steps


def test_character_ids_are_disjoint_across_cases_and_trials(scoring: ModuleType) -> None:
    cases = ["path-seed-consistent-001", "path-voice-no-rereview-001"]
    identifiers = {
        scoring.consistency_character_id(case_id=case, trial=trial)
        for case in cases
        for trial in (1, 2)
    }
    assert len(identifiers) == 4
    assert "it2-consistency-path-seed-consistent-001-t1" in identifiers


def test_fixed_corpus_loads_before_execution_and_covers_both_review_decisions(
    scoring: ModuleType,
) -> None:
    cases = scoring.load_consistency_cases(CORPUS)
    assert cases
    assert len({case["id"] for case in cases}) == len(cases)
    for operation in ("personality", "voice"):
        assert {
            case["expected"]["consistent"]
            for case in cases
            if case["level"] == "reviewer" and case["operation"] == operation
        } == {True, False}
    assert {case["operation"] for case in cases if case["level"] == "path"} >= {
        "seed",
        "temperament",
        "voice_first",
        "voice_no_rereview",
    }
    for operation in ("seed", "temperament", "voice_first"):
        assert {
            case["expected"]["consistent"]
            for case in cases
            if case["level"] == "path" and case["operation"] == operation
        } == {True, False}


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate",
        "identifier",
        "version",
        "missing_expected",
        "trial_count",
        "seed_request",
        "voice_request",
        "missing_case",
    ],
)
def test_invalid_fixed_cases_are_rejected_before_execution(
    scoring: ModuleType, tmp_path: Path, invalid: str
) -> None:
    payload = json.loads(CORPUS.read_text(encoding="utf-8"))
    if invalid == "duplicate":
        payload["cases"].append(deepcopy(payload["cases"][0]))
    elif invalid == "identifier":
        payload["cases"][0]["id"] = "path/seed 001"
    elif invalid == "version":
        payload["scorer_version"] = SCORER_VERSION + "-incompatible"
    elif invalid == "missing_expected":
        del payload["cases"][0]["expected"]
    elif invalid == "trial_count":
        payload["trial_count"] = 2
    elif invalid in {"seed_request", "voice_request"}:
        operation = "seed" if invalid == "seed_request" else "voice_first"
        case = next(case for case in payload["cases"] if case["operation"] == operation)
        case["input"] = {}
    else:
        payload["cases"] = [
            case for case in payload["cases"] if case["operation"] != "voice_no_rereview"
        ]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        scoring.load_consistency_cases(path)


def trial_rows() -> list[dict[str, object]]:
    return [
        {
            "trial": trial,
            "cases": [
                {"id": identifier, "level": level, "outcome": "correct", "reason": "合成結果"}
                for identifier, level in (("reviewer-001", "reviewer"), ("path-001", "path"))
            ],
        }
        for trial in (1, 2, 3)
    ]


def test_three_complete_trials_pass_with_separate_level_verdicts(scoring: ModuleType) -> None:
    rows = trial_rows()
    result = scoring.summarize_consistency_trials(
        rows, required_case_ids={"reviewer-001", "path-001"}
    )
    assert result["verdict"] == "PASS"
    assert result["levels"] == {"reviewer": "PASS", "path": "PASS"}
    assert result["trials"] == rows


@pytest.mark.parametrize(
    "invalid", ["two_trials", "duplicate_trial", "extra_trial", "missing_case", "failed_first"]
)
def test_missing_or_failed_trials_cannot_be_replaced_by_later_success(
    scoring: ModuleType, invalid: str
) -> None:
    rows = trial_rows()
    if invalid == "two_trials":
        rows.pop()
    elif invalid == "duplicate_trial":
        rows[1]["trial"] = 1
    elif invalid == "extra_trial":
        rows.append({**deepcopy(rows[-1]), "trial": 4})
    else:
        cases = [{"id": "reviewer-001", "level": "reviewer", "outcome": "correct"}]
        if invalid == "failed_first":
            cases.append({"id": "path-001", "level": "path", "outcome": "generation_failed"})
        rows[0]["cases"] = cases
    result = scoring.summarize_consistency_trials(
        rows, required_case_ids={"reviewer-001", "path-001"}
    )
    assert result["verdict"] == "FAIL"
    assert result["trials"] == rows
    if invalid == "failed_first":
        assert result["levels"] == {"reviewer": "PASS", "path": "FAIL"}
