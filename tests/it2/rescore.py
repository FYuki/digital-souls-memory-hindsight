"""Rescore stored trial evidence after a scoring-contract change, without model runs.

Only cases decided by stored memories are recomputed: AC-M2 and extracted AC-R3.
Results that need recall materials, which are not stored, stay as recorded.
"""

from .cases_contract import Case
from .quality import score_quality
from .quality_contract import QualityTrial
from .scoring import calculate_metrics


def _recomputable(case: Case) -> bool:
    return case["acceptance_id"] == "AC-M2" or (
        case["acceptance_id"] == "AC-R3" and case["scoring_unit"] == "extracted_fact"
    )


def rescore_trial(row: QualityTrial, cases: list[Case]) -> QualityTrial:
    selected = [case for case in cases if case["id"] in row["cases"] and _recomputable(case)]
    if row["completion"] != "complete" or not selected:
        return row
    character = f"it2-{row['suite']}-t{row['trial']}"
    scored = score_quality(
        cases=selected, character_id=character, memories=row["memories"], recalls={}
    )
    recomputed = {case["id"] for case in selected}
    failures = {key: value for key, value in row["failures"].items() if key not in recomputed}
    metrics = row["metrics"]
    if metrics is not None and row["suite"] == "difference-fact":
        counts = scored["observations"]
        metrics = {
            **metrics,
            "false_merge_rate": calculate_metrics(**counts)["false_merge_rate"],
        }
    return {
        **row,
        "cases": {**row["cases"], **scored["cases"]},
        "failures": {**failures, **scored["failures"]},
        "metrics": metrics,
    }
