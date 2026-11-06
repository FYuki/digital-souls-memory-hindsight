"""AC-I4 response/provision comparisons, using public HTTP and persisted row shapes."""

import json
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from digital_souls_memory_hindsight.recall_http import RecallResponse
from digital_souls_memory_hindsight.resident_http import ResidentResponse

pytestmark = pytest.mark.ut


@pytest.fixture
def scoring() -> ModuleType:
    return import_module("tests.it2.e2e")


def resident_pair() -> tuple[dict[str, object], dict[str, object]]:
    response = ResidentResponse.model_validate(
        {
            "provision_id": "resident-1",
            "core_identity": {"version": 1, "values": ["誠実"], "prohibitions": ["捏造"]},
            "temperament": {"version": 2, "description": "穏やか"},
            "personality_profile": {"version": 3, "description": "親切"},
            "voice_exemplars": [
                {"exemplar_id": "voice-2", "question": None, "answer": "こんにちは"},
                {"exemplar_id": "voice-1", "question": "元気？", "answer": "はい"},
            ],
            "character_profile": {
                "revision": 4,
                "entries": [
                    {"turn_id": "source-1", "start": 1, "end": 3, "text": "合成"},
                    {"turn_id": "source-2", "start": 0, "end": 2, "text": "原文"},
                ],
            },
            "motivations": [],
        }
    ).model_dump()
    record: dict[str, object] = {
        "provision_id": "resident-1",
        "kind": "resident",
        "character_id": "synthetic-character",
        "session_id": "synthetic-session",
        "surface": "surface-a",
        "core_identity_version": 1,
        "temperament_version": 2,
        "personality_version": 3,
        "exemplar_ids": ["voice-2", "voice-1"],
        "profile_revision": 4,
        "profile_entries": [
            {"turn_id": "source-1", "start": 1, "end": 3},
            {"turn_id": "source-2", "start": 0, "end": 2},
        ],
    }
    return response, record


def turn_pair() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    _, resident = resident_pair()
    response = RecallResponse.model_validate(
        {
            "provision_id": "turn-provision-1",
            "memories": [
                {
                    "id": "memory-2",
                    "kind": "world",
                    "content": "架空の記憶",
                    "source_turn_ids": ["source-1"],
                    "trust": "inferred",
                },
                {
                    "id": "memory-1",
                    "kind": "world",
                    "content": "別の架空の記憶",
                    "source_turn_ids": ["source-2"],
                    "trust": "inferred",
                },
            ],
            "source_spans": [
                {
                    "turn_id": "source-1",
                    "start": 1,
                    "end": 3,
                    "text": "合成",
                    "actor": "synthetic-user",
                    "language": "ja",
                    "truncated": False,
                    "original_start": 1,
                    "original_end": 3,
                    "trust": "evidence",
                }
            ],
            "verbatim_anchors": [
                {
                    "anchor_id": "anchor-1",
                    "turn_id": "source-1",
                    "text": "合成",
                    "trust": "evidence",
                }
            ],
            "context": None,
        }
    ).model_dump()
    record: dict[str, object] = {
        "provision_id": "turn-provision-1",
        "kind": "turn",
        "character_id": "synthetic-character",
        "session_id": "synthetic-session",
        "surface": "surface-a",
        "resident_provision_id": "resident-1",
        "temperament_version": None,
        "personality_version": None,
        "memory_ids": ["memory-2", "memory-1"],
        "source_spans": [{"turn_id": "source-1", "start": 1, "end": 3}],
        "verbatim_anchor_ids": ["anchor-1"],
        "memory_trust": ["inferred", "inferred"],
        "source_span_trust": ["evidence"],
        "verbatim_anchor_trust": ["evidence"],
    }
    return response, record, resident


def test_resident_response_matches_all_recorded_selection_fields(scoring: ModuleType) -> None:
    response, record = resident_pair()
    assert scoring.compare_resident_provision(response=response, record=record) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provision_id", "other"),
        ("kind", "turn"),
        ("core_identity_version", 2),
        ("temperament_version", 1),
        ("personality_version", 2),
        ("exemplar_ids", ["voice-1", "voice-2"]),
        ("exemplar_ids", ["voice-2"]),
        ("profile_revision", 5),
        ("profile_entries", [{"turn_id": "source-1", "start": 0, "end": 3}]),
    ],
)
def test_resident_mismatch_names_the_changed_field(
    scoring: ModuleType, field: str, value: object
) -> None:
    response, record = resident_pair()
    record[field] = value
    assert field in scoring.compare_resident_provision(response=response, record=record)


@pytest.mark.parametrize("field", ["turn_id", "start", "end"])
def test_resident_profile_entries_compare_each_source_coordinate(
    scoring: ModuleType, field: str
) -> None:
    response, record = resident_pair()
    entries = cast(list[dict[str, object]], record["profile_entries"])
    entries[0][field] = "wrong-turn" if field == "turn_id" else 9
    assert "profile_entries" in scoring.compare_resident_provision(response=response, record=record)


def test_resident_null_identity_and_profile_match_null_record_fields(scoring: ModuleType) -> None:
    response, record = resident_pair()
    for response_field, record_field in (
        ("core_identity", "core_identity_version"),
        ("temperament", "temperament_version"),
        ("personality_profile", "personality_version"),
        ("character_profile", "profile_revision"),
    ):
        response[response_field] = None
        record[record_field] = None
    response["voice_exemplars"] = []
    record["exemplar_ids"] = []
    record["profile_entries"] = []
    assert scoring.compare_resident_provision(response=response, record=record) == []
    record["profile_revision"] = 4
    assert "profile_revision" in scoring.compare_resident_provision(
        response=response, record=record
    )


def test_turn_response_matches_record_and_resolves_resident_reference(scoring: ModuleType) -> None:
    response, record, resident = turn_pair()
    assert scoring.compare_turn_provision(response=response, record=record, resident=resident) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provision_id", "other"),
        ("kind", "resident"),
        ("resident_provision_id", "other"),
        ("memory_ids", ["memory-1", "memory-2"]),
        ("source_spans", [{"turn_id": "source-1", "start": 1, "end": 4}]),
        ("verbatim_anchor_ids", []),
        ("memory_trust", ["evidence", "inferred"]),
        ("source_span_trust", ["inferred"]),
        ("verbatim_anchor_trust", ["inferred"]),
        ("temperament_version", 2),
        ("personality_version", 3),
    ],
)
def test_turn_mismatch_names_the_changed_field(
    scoring: ModuleType, field: str, value: object
) -> None:
    response, record, resident = turn_pair()
    record[field] = value
    assert field in scoring.compare_turn_provision(
        response=response, record=record, resident=resident
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "turn"), ("character_id", "other-character"), ("session_id", "other-session")],
)
def test_turn_reference_must_resolve_to_resident_for_same_character_and_session(
    scoring: ModuleType, field: str, value: object
) -> None:
    response, record, resident = turn_pair()
    resident[field] = value
    assert scoring.compare_turn_provision(response=response, record=record, resident=resident)


def test_missing_resident_reference_cannot_pass(scoring: ModuleType) -> None:
    response, record, _ = turn_pair()
    assert scoring.compare_turn_provision(response=response, record=record, resident=None)


def test_comparisons_do_not_rewrite_the_observed_evidence(scoring: ModuleType) -> None:
    response, record, resident = turn_pair()
    original = deepcopy((response, record, resident))
    scoring.compare_turn_provision(response=response, record=record, resident=resident)
    assert (response, record, resident) == original


def test_trial_characters_are_disjoint(scoring: ModuleType) -> None:
    characters = [scoring.e2e_character_id(trial=trial) for trial in (1, 2, 3)]
    assert characters == ["it2-e2e-t1", "it2-e2e-t2", "it2-e2e-t3"]


@pytest.mark.parametrize("trial", [0, 4])
def test_character_id_rejects_trials_outside_fixed_three(scoring: ModuleType, trial: int) -> None:
    with pytest.raises(ValueError):
        scoring.e2e_character_id(trial=trial)


def test_fixed_e2e_case_loads_with_current_scorer_and_three_trials(scoring: ModuleType) -> None:
    from tests.it2.scoring import SCORER_VERSION

    case = scoring.load_e2e_case(Path("tests/it2/cases/e2e.json"))
    assert case.scorer_version == SCORER_VERSION
    assert case.trial_count == 3


@pytest.mark.parametrize(
    "field", ["schema_version", "scorer_version", "trial_count", "seed", "deletion_marker"]
)
def test_invalid_e2e_case_is_rejected_before_execution(
    scoring: ModuleType,
    tmp_path: Path,
    field: str,
) -> None:
    payload = json.loads(Path("tests/it2/cases/e2e.json").read_text())
    payload[field] = {
        "schema_version": 2,
        "scorer_version": "incompatible",
        "trial_count": 2,
        "seed": {},
        "deletion_marker": "not-in-any-source",
    }[field]
    path = tmp_path / "case.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        scoring.load_e2e_case(path)


def state_row() -> dict[str, object]:
    return {
        "generation": 1,
        "state_generation": 1,
        "generated_at": "2026-11-05T00:00:00+00:00",
        "state": {"mood": "穏やか", "relationship_temperature": "温かい", "current_topic": "お茶"},
    }


@pytest.mark.parametrize(
    "violation", [None, "state_generation", "generation", "generated_at", "isolation"]
)
def test_state_update_requires_new_generation_and_preserves_other_scopes(
    scoring: ModuleType,
    violation: str | None,
) -> None:
    before = state_row()
    after = {
        **before,
        "generation": 2,
        "state_generation": 2,
        "generated_at": "2026-11-05T00:00:01+00:00",
        "state": {
            "mood": "落ち着いている",
            "relationship_temperature": "親しい",
            "current_topic": "約束",
        },
    }
    others_before = [state_row(), state_row()]
    others_after = deepcopy(others_before)
    if violation == "isolation":
        others_after[1]["generation"] = 2
    elif violation is not None:
        after[violation] = before[violation]
        if violation == "state_generation":
            after[violation] = 1
    result = scoring.compare_state_update(
        before=before, after=after, others_before=others_before, others_after=others_after
    )
    if violation is None:
        assert result == []
    else:
        assert violation in result


@pytest.mark.parametrize("field", ["mood", "relationship_temperature", "current_topic"])
@pytest.mark.parametrize("invalid", [None, " \t", "長" * 201])
def test_state_update_rejects_missing_blank_or_oversized_fields(
    scoring: ModuleType,
    field: str,
    invalid: object,
) -> None:
    before = state_row()
    after = {
        **before,
        "generation": 2,
        "state_generation": 2,
        "generated_at": "2026-11-05T00:00:01+00:00",
    }
    state = cast(dict[str, object], after["state"])
    if invalid is None:
        del state[field]
    else:
        state[field] = invalid
    assert "state" in scoring.compare_state_update(
        before=before,
        after=after,
        others_before=[],
        others_after=[],
    )


@pytest.mark.parametrize(
    "remaining",
    [None, "document", "memory", "source_memory", "text", "source_span", "profile_entry"],
)
def test_deletion_rejects_each_remaining_memory_or_provision_source(
    scoring: ModuleType,
    remaining: str | None,
) -> None:
    memory = {
        "id": "control-memory",
        "text": "対照",
        "document_id": "control-document",
        "source_memory_ids": [],
    }
    record: dict[str, object] = {"kind": "resident", "source_spans": [], "profile_entries": []}
    if remaining == "document":
        memory["document_id"] = "deleted-document"
    elif remaining == "memory":
        memory["id"] = "deleted-memory"
    elif remaining == "source_memory":
        memory["source_memory_ids"] = ["deleted-memory"]
    elif remaining == "text":
        memory["text"] = "消去対象を含む"
    elif remaining in {"source_span", "profile_entry"}:
        record["source_spans" if remaining == "source_span" else "profile_entries"] = [
            {"turn_id": "deleted-turn", "start": 0, "end": 2},
        ]
    result = scoring.deletion_residuals(
        memories=[memory],
        provisions=[record],
        deleted_turn_ids={"deleted-turn"},
        deleted_document_id="deleted-document",
        deleted_memory_ids={"deleted-memory"},
        marker="消去対象",
    )
    assert result == (
        []
        if remaining is None
        else ["provisions" if remaining in {"source_span", "profile_entry"} else "memories"]
    )


def test_deletion_handles_turn_records_with_null_resident_only_columns(scoring: ModuleType) -> None:
    assert (
        scoring.deletion_residuals(
            memories=[],
            provisions=[{"kind": "turn", "source_spans": [], "profile_entries": None}],
            deleted_turn_ids={"deleted-turn"},
            deleted_document_id="deleted-document",
            deleted_memory_ids=set(),
            marker="消去対象",
        )
        == []
    )
