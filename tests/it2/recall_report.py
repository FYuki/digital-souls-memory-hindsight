"""Generate recall tables from recorded trials without trusting stored verdicts."""

import argparse
import html
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from .cases_contract import load_cases
from .performance import Series, recompute_series, summarize_performance
from .quality_contract import GENERATION_MODELS, empty_metric_counts
from .report import validate_safe
from .scoring import SCORER_VERSION, TrialResult, score_trial, summarize_trials
from .threshold import (
    THRESHOLDS,
    AbstentionTrial,
    evaluate_candidates,
    score_responses,
    select_threshold,
)


def _cell(value: object) -> str:
    text = "未定義" if value is None else str(value)
    return html.escape(text).replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")


def generate_recall_report(paths: Sequence[Path], output: Path) -> Path:
    cases = [
        c for c in load_cases(Path(__file__).with_name("cases")) if c["acceptance_id"] == "AC-R4"
    ]
    required = {c["id"] for c in cases if c["split"] == "final"}
    reports: dict[tuple[str, str], dict[str, object]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_safe(payload)
        if not isinstance(payload, dict) or payload.get("scorer_version") != SCORER_VERSION:
            raise ValueError("Invalid recall result version")
        if set(payload) - {
            "model",
            "scorer_version",
            "threshold",
            "verdict",
            "trials",
            "series",
            "evaluations",
            "guideline_met",
            "reason",
        }:
            raise ValueError("Unknown recall result fields")
        model = payload.get("model")
        if model not in GENERATION_MODELS:
            raise ValueError("Unknown generation model")
        kind = next(
            (
                name
                for name in ("threshold-selection", "threshold-confirmation", "performance")
                if path.name == f"recall-{name}-{model}.json"
            ),
            None,
        )
        if kind is None or (model, kind) in reports:
            raise ValueError("Unknown or duplicate recall result")
        reports[model, kind] = payload
    lines = ["# 想起の閾値と性能", "", "| モデル | 試験 | 判定 |", "| --- | --- | --- |"]
    details: list[str] = []
    for model in GENERATION_MODELS:
        for kind in ("threshold-selection", "threshold-confirmation", "performance"):
            payload = reports.get((model, kind))
            verdict = "NOT RUN"
            if payload is not None and kind == "threshold-selection":
                trials: list[AbstentionTrial] = TypeAdapter(list[AbstentionTrial]).validate_python(
                    payload["trials"], strict=True
                )
                table = evaluate_candidates(cases=cases, trials=trials, thresholds=THRESHOLDS)
                selection = select_threshold(table)
                verdict = selection["verdict"]
                details += [
                    "",
                    f"## {model} 閾値候補",
                    "",
                    f"選択値: {_cell(selection['threshold'])} / "
                    f"目安達成: {selection['guideline_met']}",
                    "",
                    "| 閾値 | 再現率 | 棄権率 | 必須通過 |",
                    "| --- | --- | --- | --- |",
                ]
                for row in table:
                    details.append(
                        "| "
                        + " | ".join(
                            _cell(v)
                            for v in (
                                row["threshold"],
                                row["metrics"]["recall"],
                                row["metrics"]["abstention_rate"],
                                row["required_passed"],
                            )
                        )
                        + " |"
                    )
            elif payload is not None and kind == "threshold-confirmation":
                raw_trials: list[AbstentionTrial] = TypeAdapter(
                    list[AbstentionTrial]
                ).validate_python(payload["trials"], strict=True)
                scored_trials: list[TrialResult] = []
                for raw in raw_trials:
                    outcomes = {case["id"]: False for case in cases}
                    observations = empty_metric_counts()
                    if raw["completion"] == "complete":
                        responses = {
                            query["case_id"]: query["response"] for query in raw["queries"]
                        }
                        if len(raw["queries"]) != len(cases) or set(responses) != {
                            case["id"] for case in cases
                        }:
                            raise ValueError("Confirmation query set differs")
                        scored = score_responses(
                            cases=cases, character_id=raw["character_id"], recalls=responses
                        )
                        outcomes, observations = scored["cases"], scored["observations"]
                    scored_trials.append(
                        score_trial(
                            bank_id=raw["bank_id"],
                            completion=raw["completion"],
                            cases=outcomes,
                            observations=observations,
                        )
                    )
                verdict = str(
                    summarize_trials(scored_trials, required_case_ids=required)["verdict"]
                )
                details += [
                    "",
                    f"## {model} 最終確認",
                    "",
                    f"閾値: {_cell(payload['threshold'])}",
                    "",
                    "| bank | 完了 | 必須ケース | 再現率 | 棄権率 | "
                    "Anchor混入率 | Fact空 | 全体空 |",
                    "| --- | --- | --- | --- | --- | --- | --- | --- |",
                ]
                for trial in scored_trials:
                    metrics = trial["metrics"]
                    values: list[object] = [
                        trial["bank_id"],
                        trial["completion"],
                        "; ".join(
                            f"{key}={'PASS' if trial['cases'].get(key) is True else 'FAIL'}"
                            for key in sorted(required)
                        ),
                    ]
                    metrics_map = {} if metrics is None else dict(metrics)
                    values.extend(
                        None if metrics is None else metrics_map[key]
                        for key in (
                            "recall",
                            "abstention_rate",
                            "anchor_contamination_rate",
                            "empty_fact_responses",
                            "empty_responses",
                        )
                    )
                    details.append("| " + " | ".join(_cell(v) for v in values) + " |")
            elif payload is not None:
                series: list[Series] = TypeAdapter(list[Series]).validate_python(
                    payload["series"], strict=True
                )
                verdict = str(summarize_performance(series)["verdict"])
                details += [
                    "",
                    f"## {model} 性能",
                    "",
                    "| 対象件数 | 判定 | 件数 | p50 ms | p95 ms | 失敗率 | "
                    "正例Fact非空 | 正例全体非空 | Fact非空 | 全体非空 | "
                    "p50目標 | p95目標 | 条件 |",
                    "| --- | --- | --- | --- | --- | --- | --- | "
                    "--- | --- | --- | --- | --- | --- |",
                ]
                for performance_row in series:
                    actual = recompute_series(performance_row)
                    details.append(
                        "| "
                        + " | ".join(
                            _cell(v)
                            for v in (
                                performance_row["target_memories"],
                                actual["verdict"],
                                performance_row["counts"],
                                actual["p50_ms"],
                                actual["p95_ms"],
                                actual["failure_rate"],
                                actual["positive_fact_responses"],
                                actual["positive_nonempty_responses"],
                                actual["fact_responses"],
                                actual["nonempty_responses"],
                                actual["target_p50_met"],
                                actual["target_p95_met"],
                                performance_row["conditions"],
                            )
                        )
                        + " |"
                    )
                    for key in (
                        "preparation",
                        "separate_measurements",
                        "generation",
                        "trace_durations",
                    ):
                        details.append(f"{key}: {_cell(performance_row.get(key))}")
            lines.append(f"| {model} | {kind} | {verdict} |")
    text = "\n".join(
        [
            *lines,
            *details,
            "",
            "再現率90%は選択の目安。Anchor混入率とp50 150ms・p95 300msは記録用。",
            "",
        ]
    )
    validate_safe(text)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        target.write(text)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate recall threshold and performance tables")
    parser.add_argument("--report", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        generate_recall_report(cast(list[Path], args.report), args.output)
    except (ValueError, OSError, KeyError):
        print("Recall report rejected; no safe new report was generated")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
