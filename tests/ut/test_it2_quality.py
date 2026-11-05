"""Score synthetic SDK rows and service recall responses, without a model."""

from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest
from hindsight_client_api.models.memory_unit_list_item import MemoryUnitListItem

from digital_souls_memory_hindsight.evidence.projection import document_id
from digital_souls_memory_hindsight.recall_http import RecallResponse
from tests.it2.cases_contract import Case, load_cases
from tests.it2.scoring import calculate_metrics

pytestmark = pytest.mark.ut
CORPUS = Path(__file__).resolve().parents[1] / "it2/cases"
CHARACTER = "it2-same-fact-t1"


@pytest.fixture
def quality() -> ModuleType:
    return import_module("tests.it2.quality")


def _case(identifier: str) -> Case:
    return next(case for case in load_cases(CORPUS) if case["id"] == identifier)


def _memory(
    identifier: str, text: str, session: str | None, sources: list[str] | None
) -> dict[str, object]:
    return dict(
        MemoryUnitListItem(
            id=identifier,
            text=text,
            fact_type="observation" if sources is not None else "world",
            document_id=document_id(f"{CHARACTER}-{session}") if session else None,
            source_memory_ids=sources,
        ).to_dict()
    )


def _recall(identifier: str | None, mentions: list[str], text: str) -> dict[str, object]:
    turns = [f"{CHARACTER}-{mention}" for mention in mentions]
    return RecallResponse.model_validate(
        {
            "provision_id": "synthetic-provision",
            "memories": [
                {
                    "id": identifier,
                    "kind": "observation",
                    "content": text,
                    "source_turn_ids": turns,
                    "trust": "inferred",
                }
            ]
            if identifier
            else [],
            "source_spans": [
                {
                    "turn_id": turn,
                    "start": 0,
                    "end": len(text),
                    "text": text,
                    "actor": CHARACTER,
                    "language": "ja",
                    "truncated": False,
                    "original_start": 0,
                    "original_end": len(text),
                    "trust": "evidence",
                }
                for turn in turns
            ],
            "verbatim_anchors": [],
            "context": None,
        }
    ).model_dump()


def _same_rows() -> list[dict[str, object]]:
    return [
        _memory("f1", "澪は星見町の天文台で働く", "same-s1", None),
        _memory("f2", "星見町の天文台が澪の勤務先", "same-s2", None),
        _memory("o1", "澪の勤務先は星見町の天文台", None, ["f1", "f2"]),
    ]


def test_same_fact_keeps_all_mentions_and_is_recalled(quality: ModuleType) -> None:
    case = _case("same-fact-001")
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=_same_rows(),
        recalls={case["id"]: _recall("o1", ["same-m1", "same-m2"], "勤務先")},
    )
    assert result["cases"] == {case["id"]: True}
    assert result["failures"] == {}
    assert result["observations"]["same_fact_groups"] == 1
    assert result["observations"]["unmerged_same_fact_groups"] == 0
    metrics = calculate_metrics(**result["observations"])
    assert metrics["missed_merge_rate"] == 0
    assert metrics["false_merge_rate"] is None


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        ([], "not_consolidated"),
        ([("o1", ["f1"])], "missing_source_mentions"),
        ([("o1", ["f1"]), ("o2", ["f2"])], "split_consolidation"),
        ([("o1", ["unknown-fact"])], "missing_source_mentions"),
    ],
)
def test_same_fact_rejects_incomplete_or_split_lineage(
    quality: ModuleType, observations: list[tuple[str, list[str]]], reason: str
) -> None:
    case = _case("same-fact-001")
    rows = _same_rows()[:2] + [
        _memory(identifier, "勤務先", None, sources) for identifier, sources in observations
    ]
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=rows,
        recalls={case["id"]: _recall("o1", ["same-m1", "same-m2"], "勤務先")},
    )
    assert result["cases"] == {case["id"]: False}
    assert result["failures"] == {case["id"]: reason}
    assert result["observations"]["unmerged_same_fact_groups"] == 1


@pytest.mark.parametrize(
    ("identifier", "mentions", "reason"),
    [(None, [], "not_recalled"), ("o1", ["same-m1"], "missing_source_mentions")],
)
def test_correct_lineage_still_requires_recall_with_all_sources(
    quality: ModuleType, identifier: str | None, mentions: list[str], reason: str
) -> None:
    case = _case("same-fact-001")
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=_same_rows(),
        recalls={case["id"]: _recall(identifier, mentions, "勤務先")},
    )
    assert result["cases"][case["id"]] is False
    assert result["failures"][case["id"]] == reason
    assert result["observations"]["unmerged_same_fact_groups"] == 0


@pytest.mark.parametrize("difference", ["number", "negation", "subject", "tense"])
def test_distinct_facts_remain_separate_for_each_fixed_difference(
    quality: ModuleType, difference: str
) -> None:
    case = _case(f"difference-{difference}-001")
    session = case["input"]["sessions"][0]["session_id"]
    rows = [
        _memory(f"f{i}", text, session, None) for i, text in enumerate(case["expected"]["facts"])
    ] + [
        _memory(f"o{i}", text, None, [f"f{i}"]) for i, text in enumerate(case["expected"]["facts"])
    ]
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=rows,
        recalls={},
    )
    assert result["cases"] == {case["id"]: True}
    assert result["failures"] == {}
    assert result["observations"]["different_fact_pairs"] == 1
    assert result["observations"]["merged_different_fact_pairs"] == 0


@pytest.mark.parametrize("merge_in", ["lineage", "fact_text", "observation_text"])
def test_distinct_pair_merged_in_text_or_lineage_fails_once(
    quality: ModuleType, merge_in: str
) -> None:
    case = _case("difference-number-001")
    session = "difference-number-s1"
    rows = [
        _memory("f1", "青い石は三個", session, None),
        _memory("f2", "青い石は五個", session, None),
        _memory("o1", "青い石は三個", None, ["f1"]),
        _memory("o2", "青い石は五個", None, ["f2"]),
    ]
    if merge_in == "lineage":
        rows += [_memory("o3", "青い石", None, ["f1", "f2"])]
    elif merge_in == "fact_text":
        rows += [_memory("f3", "青い石は三個または五個", session, None)]
    else:
        rows += [_memory("o3", "青い石は三個または五個", None, ["f1"])]
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=rows,
        recalls={},
    )
    assert result["cases"] == {case["id"]: False}
    assert result["failures"] == {case["id"]: "false_merge"}
    assert result["observations"]["merged_different_fact_pairs"] == 1
    assert calculate_metrics(**result["observations"])["false_merge_rate"] == 1


def test_english_negation_does_not_match_the_positive_side(quality: ModuleType) -> None:
    case = _case("difference-negation-001")
    texts = ["Mio does not drink yuzu tea", "Mio drinks yuzu tea"]
    rows = [
        _memory(f"f{i}", text, "difference-negation-s1", None) for i, text in enumerate(texts)
    ] + [_memory(f"o{i}", text, None, [f"f{i}"]) for i, text in enumerate(texts)]
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=rows,
        recalls={},
    )
    assert result["cases"] == {case["id"]: True}
    assert result["observations"]["merged_different_fact_pairs"] == 0


@pytest.mark.parametrize(
    ("identifier", "text", "reason"),
    [
        ("difference-negation-001", "澪は柚子茶を飲まない", "fact_lost"),
        ("difference-number-001", "澪は青い石を保管している", "unresolved_fact"),
    ],
)
def test_missing_or_unclassifiable_side_is_not_a_false_merge(
    quality: ModuleType, identifier: str, text: str, reason: str
) -> None:
    case = _case(identifier)
    session = case["input"]["sessions"][0]["session_id"]
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=[_memory("f1", text, session, None), _memory("o1", text, None, ["f1"])],
        recalls={},
    )
    assert result["cases"] == {identifier: False}
    assert result["failures"] == {identifier: reason}
    assert result["observations"]["merged_different_fact_pairs"] == 0


@pytest.mark.parametrize("feature", ["proper_name", "date", "verbatim"])
@pytest.mark.parametrize("lost_in", [None, "fact", "source", "both"])
def test_japanese_fact_and_returned_source_are_scored_independently(
    quality: ModuleType, feature: str, lost_in: str | None
) -> None:
    cases = [_case(f"japanese-{feature}-{unit}") for unit in ("extracted_fact", "returned_source")]
    text = cases[0]["input"]["sessions"][0]["mentions"][0]["text"]
    fact_text = "unpreserved translation" if lost_in in {"fact", "both"} else text
    if feature == "date" and lost_in in {"fact", "both"}:
        fact_text = "凪との約束は2026年11月5日だ。"
    source_text = "unpreserved translation" if lost_in in {"source", "both"} else text
    result = quality.score_quality(
        cases=cases,
        character_id=CHARACTER,
        memories=[_memory("f1", fact_text, f"ja-{feature}-s1", None)],
        recalls={case["id"]: _recall("f1", [f"ja-{feature}-m1"], source_text) for case in cases},
    )
    expected = {
        cases[0]["id"]: lost_in not in {"fact", "both"},
        cases[1]["id"]: lost_in not in {"source", "both"},
    }
    assert result["cases"] == expected
    assert result["failures"] == {
        case["id"]: (
            "extracted_fact_not_preserved"
            if case["scoring_unit"] == "extracted_fact"
            else "source_not_returned"
        )
        for case in cases
        if not expected[case["id"]]
    }
    metrics = calculate_metrics(**result["observations"])
    assert metrics["false_merge_rate"] is None
    assert metrics["missed_merge_rate"] is None


def test_observation_or_other_document_cannot_replace_japanese_extraction(
    quality: ModuleType,
) -> None:
    case = _case("japanese-proper_name-extracted_fact")
    rows = [
        _memory("f1", "Hoshimi", "ja-proper_name-s1", None),
        _memory("f2", "星見町", "unrelated-session", None),
        _memory("o1", "星見町", None, ["f1"]),
    ]
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=rows,
        recalls={},
    )
    assert result["cases"] == {case["id"]: False}
    assert result["failures"] == {case["id"]: "extracted_fact_not_preserved"}


def test_source_text_from_another_turn_cannot_satisfy_japanese_preservation(
    quality: ModuleType,
) -> None:
    case = _case("japanese-date-returned_source")
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=[],
        recalls={case["id"]: _recall("f1", ["other-turn"], "二〇二六年十一月五日")},
    )
    assert result["cases"] == {case["id"]: False}
    assert result["failures"] == {case["id"]: "source_not_returned"}


def test_unknown_difference_case_is_rejected_instead_of_assumed_pass(quality: ModuleType) -> None:
    case: Case = {**_case("difference-number-001"), "id": "unmapped-difference-case"}
    with pytest.raises(ValueError):
        quality.score_quality(cases=[case], character_id=CHARACTER, memories=[], recalls={})


@pytest.mark.parametrize("has_source", [False, True])
def test_empty_fact_response_does_not_imply_empty_materials(
    quality: ModuleType, has_source: bool
) -> None:
    case = _case("same-fact-001")
    result = quality.score_quality(
        cases=[case],
        character_id=CHARACTER,
        memories=_same_rows(),
        recalls={case["id"]: _recall(None, ["same-m1"] if has_source else [], "合成原文")},
    )
    assert result["observations"]["empty_fact_responses"] == 1
    assert result["observations"]["empty_responses"] == (0 if has_source else 1)
