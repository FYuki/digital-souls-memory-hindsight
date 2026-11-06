"""Fixed AC-I4 inputs and comparisons, independent of external execution."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from digital_souls_memory_hindsight.character_state.domain import (
    InvalidStateOutput,
    validate_output,
)
from digital_souls_memory_hindsight.identity_http import (
    CoreRequest,
    SeedRequest,
    TemperamentRequest,
)

from .quality_contract import MemoryRow
from .scoring import SCORER_VERSION

STEPS = (
    "prerequisites",
    "session_turn_receipt",
    "resident_first",
    "resident_after_change",
    "turn_recall",
    "character_state",
    "author_deletion",
)


class E2ECase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    scorer_version: str
    trial_count: Literal[3]
    cores: tuple[CoreRequest, CoreRequest]
    temperaments: tuple[TemperamentRequest, TemperamentRequest]
    seed: SeedRequest
    voice_answers: tuple[str, str]
    source_text: str = Field(min_length=1)
    main_text: str = Field(min_length=1)
    reply_text: str = Field(min_length=1)
    surface_text: str = Field(min_length=1)
    other_text: str = Field(min_length=1)
    deletion_marker: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.scorer_version != SCORER_VERSION:
            raise ValueError("Incompatible E2E scorer")
        if not all(text.strip() for text in self.voice_answers):
            raise ValueError("Voice answers must be nonblank")
        if not all(
            self.deletion_marker in text
            for text in (
                self.main_text,
                self.reply_text,
                self.surface_text,
            )
        ) or any(self.deletion_marker in text for text in (self.source_text, self.other_text)):
            raise ValueError("Deletion marker must distinguish targets from controls")
        return self


def load_e2e_case(path: Path) -> E2ECase:
    return E2ECase.model_validate(json.loads(path.read_text(encoding="utf-8")))


def e2e_character_id(*, trial: int) -> str:
    if trial not in (1, 2, 3):
        raise ValueError("E2E requires exactly three trials")
    return f"it2-e2e-t{trial}"


def _spans(values: object) -> list[dict[str, object]]:
    return [
        {key: row[key] for key in ("turn_id", "start", "end")}
        for row in cast(Sequence[Mapping[str, object]], values)
    ]


def compare_resident_provision(
    *, response: Mapping[str, object], record: Mapping[str, object]
) -> list[str]:
    expected: dict[str, object] = {"provision_id": response["provision_id"], "kind": "resident"}
    for field, saved in (
        ("core_identity", "core_identity_version"),
        ("temperament", "temperament_version"),
        ("personality_profile", "personality_version"),
    ):
        value = cast(Mapping[str, object] | None, response[field])
        expected[saved] = None if value is None else value["version"]
    expected["exemplar_ids"] = [
        row["exemplar_id"]
        for row in cast(Sequence[Mapping[str, object]], response["voice_exemplars"])
    ]
    profile = cast(Mapping[str, object] | None, response["character_profile"])
    expected["profile_revision"] = None if profile is None else profile["revision"]
    expected["profile_entries"] = [] if profile is None else _spans(profile["entries"])
    return [field for field, value in expected.items() if record[field] != value]


def compare_turn_provision(
    *,
    response: Mapping[str, object],
    record: Mapping[str, object],
    resident: Mapping[str, object] | None,
) -> list[str]:
    expected: dict[str, object] = {
        "provision_id": response["provision_id"],
        "kind": "turn",
        "temperament_version": None,
        "personality_version": None,
        "source_spans": _spans(response["source_spans"]),
    }
    for field, identifier, ids, trust in (
        ("memories", "id", "memory_ids", "memory_trust"),
        ("source_spans", "turn_id", None, "source_span_trust"),
        ("verbatim_anchors", "anchor_id", "verbatim_anchor_ids", "verbatim_anchor_trust"),
    ):
        rows = cast(Sequence[Mapping[str, object]], response[field])
        if ids is not None:
            expected[ids] = [row[identifier] for row in rows]
        expected[trust] = [row["trust"] for row in rows]
    mismatches = [field for field, value in expected.items() if record[field] != value]
    if resident is None or (
        resident["provision_id"] != record["resident_provision_id"]
        or resident["kind"] != "resident"
        or any(resident[key] != record[key] for key in ("character_id", "session_id"))
    ):
        mismatches.append("resident_provision_id")
    return mismatches


def compare_state_update(
    *,
    before: Mapping[str, object],
    after: Mapping[str, object],
    others_before: Sequence[Mapping[str, object]],
    others_after: Sequence[Mapping[str, object]],
) -> list[str]:
    mismatches = []
    try:
        validate_output(after["state"])
    except InvalidStateOutput:
        mismatches.append("state")
    if after["generation"] != after["state_generation"]:
        mismatches.append("state_generation")
    if cast(int, after["generation"]) <= cast(int, before["generation"]):
        mismatches.append("generation")
    if (
        after["generated_at"] is None
        or before["generated_at"] is None
        or (cast(str, after["generated_at"]) <= cast(str, before["generated_at"]))
    ):
        mismatches.append("generated_at")
    if others_before != others_after:
        mismatches.append("isolation")
    return mismatches


def deletion_residuals(
    *,
    memories: Sequence[MemoryRow],
    provisions: Sequence[Mapping[str, object]],
    deleted_turn_ids: set[str],
    deleted_document_id: str,
    deleted_memory_ids: set[str],
    marker: str,
) -> list[str]:
    residuals = []
    for row in memories:
        if (
            row.get("document_id") == deleted_document_id
            or row["id"] in deleted_memory_ids
            or deleted_memory_ids.intersection(row.get("source_memory_ids") or [])
            or marker in (row.get("text") or "")
        ):
            residuals.append("memories")
            break
    for record in provisions:
        spans = _spans(record["source_spans"])
        if record["kind"] == "resident":
            spans.extend(_spans(record["profile_entries"]))
        if any(span["turn_id"] in deleted_turn_ids for span in spans):
            residuals.append("provisions")
            break
    return residuals
