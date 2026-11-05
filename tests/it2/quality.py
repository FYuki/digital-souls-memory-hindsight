"""Deterministic quality scoring for the fixed synthetic extraction corpus."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from digital_souls_memory_hindsight.evidence.projection import document_id

from .cases_contract import Case
from .quality_contract import MemoryRow, empty_metric_counts
from .scoring import MetricCounts


class RecalledMemory(TypedDict):
    id: str
    source_turn_ids: list[str]


class ReturnedSpan(TypedDict):
    turn_id: str
    text: str


class RecallData(TypedDict):
    memories: list[RecalledMemory]
    source_spans: list[ReturnedSpan]
    verbatim_anchors: NotRequired[list[dict[str, object]]]
    context: NotRequired[str | None]


class QualityResult(TypedDict):
    cases: dict[str, bool]
    failures: dict[str, str]
    observations: MetricCounts


@dataclass(frozen=True)
class _Decision:
    reason: str | None
    missed_merge: bool


DIFFERENCE_FEATURES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "difference-number-001": (("三個", "3個", "three"), ("五個", "5個", "five")),
    "difference-negation-001": (
        ("飲む", "drinks", "drink"),
        ("飲まない", "does not drink", "doesn't drink", "not drink"),
    ),
    "difference-subject-001": (("澪", "mio"), ("凪", "nagi")),
    "difference-tense-001": (
        ("昨日", "yesterday", "働いていた", "2026-11-04", "2026年11月4日"),
        ("明日", "tomorrow", "予定", "2026-11-06", "2026年11月6日"),
    ),
}
# Two different people cannot be an update of one value.
NEVER_UPDATED = frozenset({"difference-subject-001"})
# Forms of one date. The returned source is still compared with the original form.
EQUIVALENT_DATES: dict[str, tuple[str, ...]] = {
    "二〇二六年十一月五日": ("二〇二六年十一月五日", "2026-11-05", "2026年11月5日"),
}


def _sides(text: str, features: tuple[tuple[str, ...], tuple[str, ...]]) -> frozenset[int]:
    remaining = text.casefold()
    sides: set[int] = set()
    candidates = sorted(
        ((token.casefold(), side) for side, tokens in enumerate(features) for token in tokens),
        key=lambda candidate: len(candidate[0]),
        reverse=True,
    )
    for token, side in candidates:
        pattern = re.escape(token)
        if token.isascii():
            pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
        # Mask the longer negative phrase before testing its positive substring.
        remaining, count = re.subn(pattern, lambda match: " " * len(match[0]), remaining)
        if count:
            sides.add(side)
    return frozenset(sides)


def _documents(case: Case, character: str) -> frozenset[str]:
    return frozenset(
        document_id(f"{character}-{session['session_id']}") for session in case["input"]["sessions"]
    )


def _same_fact(
    case: Case, character: str, memories: Sequence[MemoryRow], recall: RecallData
) -> _Decision:
    observations = [row for row in memories if row.get("fact_type") == "observation"]
    if not observations:
        return _Decision("not_consolidated", True)
    documents = _documents(case, character)
    source_documents = {
        row["id"]: row.get("document_id")
        for row in memories
        if row.get("fact_type") != "observation"
    }
    coverage = [
        (row, {source_documents.get(source) for source in row.get("source_memory_ids") or []})
        for row in observations
    ]
    matching = [(row, covered) for row, covered in coverage if covered & documents]
    if len(matching) > 1:
        return _Decision("split_consolidation", True)
    if not matching or not documents <= matching[0][1]:
        return _Decision("missing_source_mentions", True)
    observation = matching[0][0]
    recalled = [row for row in recall["memories"] if row["id"] == observation["id"]]
    if not recalled:
        return _Decision("not_recalled", False)
    expected_turns = {
        f"{character}-{mention}" for mention in case["expected"]["source_mention_ids"]
    }
    actual_turns = {turn for row in recalled for turn in row["source_turn_ids"]}
    return _Decision(None if expected_turns <= actual_turns else "missing_source_mentions", False)


def _different_facts(case: Case, character: str, memories: Sequence[MemoryRow]) -> _Decision:
    if case["id"] not in DIFFERENCE_FEATURES:
        raise ValueError("Difference case has no fixed classification features")
    features = DIFFERENCE_FEATURES[case["id"]]
    documents = _documents(case, character)
    facts = {
        row["id"]: row
        for row in memories
        if row.get("fact_type") != "observation" and row.get("document_id") in documents
    }
    classified = {
        identifier: _sides(row.get("text") or "", features) for identifier, row in facts.items()
    }
    if any(len(sides) == 2 for sides in classified.values()):
        return _Decision("false_merge", False)
    sourced = [
        (row, set(row.get("source_memory_ids") or []) & facts.keys())
        for row in memories
        if row.get("fact_type") == "observation"
    ]
    sourced = [(row, sources) for row, sources in sourced if sources]
    if case["id"] in NEVER_UPDATED:
        for observation, sources in sourced:
            sides = set(_sides(observation.get("text") or "", features))
            for source in sources:
                sides.update(classified[source])
            if len(sides) == 2:
                return _Decision("false_merge", False)
    covered = {side for sides in classified.values() for side in sides}
    if covered != {0, 1}:
        reason = (
            "unresolved_fact" if any(not sides for sides in classified.values()) else "fact_lost"
        )
        return _Decision(reason, False)
    # A later value or a stated history is allowed; a value from neither Fact is not.
    if any(not _sides(row.get("text") or "", features) for row, _ in sourced):
        return _Decision("observation_value_unresolved", False)
    return _Decision(None, False)


def _japanese(
    case: Case, character: str, memories: Sequence[MemoryRow], recall: RecallData | None
) -> _Decision:
    preserve = case["expected"]["preserve"]
    if case["scoring_unit"] == "extracted_fact":
        documents = _documents(case, character)
        texts = [
            row.get("text") or ""
            for row in memories
            if row.get("fact_type") != "observation" and row.get("document_id") in documents
        ]
        reason = "extracted_fact_not_preserved"
        forms = [EQUIVALENT_DATES.get(token, (token,)) for token in preserve]
        preserved = any(
            all(any(form in text for form in options) for options in forms) for text in texts
        )
        return _Decision(None if preserved else reason, False)
    elif case["scoring_unit"] == "returned_source":
        if recall is None:
            raise ValueError("Returned-source scoring requires recall materials")
        turns = {
            f"{character}-{mention['id']}"
            for session in case["input"]["sessions"]
            for mention in session["mentions"]
        }
        texts = [span["text"] for span in recall["source_spans"] if span["turn_id"] in turns]
        reason = "source_not_returned"
    else:
        raise ValueError("Unknown Japanese scoring unit")
    preserved = any(all(token in text for token in preserve) for text in texts)
    return _Decision(None if preserved else reason, False)


def score_quality(
    *,
    cases: Sequence[Case],
    character_id: str,
    memories: Sequence[MemoryRow],
    recalls: dict[str, RecallData],
) -> QualityResult:
    counts = empty_metric_counts()
    for recall in recalls.values():
        if not recall["memories"]:
            counts["empty_fact_responses"] += 1
            if (
                not recall["source_spans"]
                and not recall.get("verbatim_anchors")
                and not recall.get("context")
            ):
                counts["empty_responses"] += 1
    outcomes: dict[str, bool] = {}
    failures: dict[str, str] = {}
    for case in cases:
        if case["acceptance_id"] == "AC-M1":
            decision = _same_fact(case, character_id, memories, recalls[case["id"]])
            counts["same_fact_groups"] += 1
            counts["unmerged_same_fact_groups"] += int(decision.missed_merge)
        elif case["acceptance_id"] == "AC-M2":
            decision = _different_facts(case, character_id, memories)
            counts["different_fact_pairs"] += 1
            counts["merged_different_fact_pairs"] += int(decision.reason == "false_merge")
        elif case["acceptance_id"] == "AC-R3":
            decision = _japanese(case, character_id, memories, recalls.get(case["id"]))
        else:
            raise ValueError("Case is outside extraction quality scope")
        outcomes[case["id"]] = decision.reason is None
        if decision.reason is not None:
            failures[case["id"]] = decision.reason
    return {"cases": outcomes, "failures": failures, "observations": counts}
