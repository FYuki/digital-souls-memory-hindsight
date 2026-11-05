"""Compare validated public reports without selecting a generation model."""

import argparse
import html
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from pydantic import TypeAdapter

from .cases_contract import Case, load_cases
from .quality_contract import (
    GENERATION_MODELS,
    QualityTrial,
    Suite,
    quality_suites,
    quality_verdict,
)
from .report import ALLOWED_FIELDS, validate_safe
from .scoring import SCORER_VERSION


class _Report(TypedDict):
    models: dict[str, object]
    scorer_version: str
    trials: list[QualityTrial]


_REPORT: TypeAdapter[_Report] = TypeAdapter(_Report)


def _load_report(path: Path, suites: dict[Suite, list[Case]]) -> tuple[str, list[QualityTrial]]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    validate_safe(payload)
    if not isinstance(payload, dict) or set(payload) != ALLOWED_FIELDS:
        raise ValueError("Input must contain the approved public report fields")
    report = _REPORT.validate_python(payload, strict=True)
    model = report["models"].get("generation")
    if not isinstance(model, str) or model not in GENERATION_MODELS:
        raise ValueError("Unknown generation model")
    if report["scorer_version"] != SCORER_VERSION:
        raise ValueError("Scorer versions differ")
    rows = report["trials"]
    if (
        len(rows) != 9
        or {(row["suite"], row["trial"]) for row in rows}
        != {(suite, trial) for suite in suites for trial in range(1, 4)}
        or len({row["bank_id"] for row in rows}) != 9
    ):
        raise ValueError("Every suite requires three independent trials")
    for row in rows:
        required = {case["id"] for case in suites[row["suite"]]}
        if set(row["cases"]) != required or not set(row["failures"]) <= required:
            raise ValueError("Required case sets differ")
        if set(row["failures"]) != {
            identifier for identifier, passed in row["cases"].items() if not passed
        }:
            raise ValueError("Failure details must match failed cases")
        if row["completion"] not in {"complete", "waiting", "failed", "timeout"}:
            raise ValueError("Unknown completion status")
        if row["elapsed_seconds"] < 0:
            raise ValueError("Elapsed time must be nonnegative")
        if (row["completion"] == "complete") != (row["metrics"] is not None):
            raise ValueError("Metrics require completed work")
        if row["metrics"] is not None:
            for name in (
                "false_merge_rate",
                "missed_merge_rate",
                "recall",
                "abstention_rate",
                "anchor_contamination_rate",
            ):
                value = row["metrics"][name]
                if value is not None and not 0 <= value <= 1:
                    raise ValueError("Rates must be between zero and one")
    return model, rows


def _cell(value: str) -> str:
    return html.escape(value).replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")


def _rate(value: float | None) -> str:
    return "未定義" if value is None else f"{value:.2%}"


def generate_comparison(paths: Sequence[Path], output: Path) -> Path:
    suites = quality_suites(load_cases(Path(__file__).with_name("cases")))
    reports: dict[str, list[QualityTrial]] = {}
    for path in paths:
        model, rows = _load_report(path, suites)
        if model in reports:
            raise ValueError("Duplicate generation model reports")
        reports[model] = rows
    verdicts = {model: quality_verdict(rows, suites) for model, rows in reports.items()}
    lines = ["# 抽出・統合の品質比較", "", "| モデル | 判定 |", "| --- | --- |"]
    for model in GENERATION_MODELS:
        lines.append(f"| {model} | {verdicts.get(model, 'NOT RUN')} |")
    lines += [
        "",
        "| モデル | suite | 試行 | 判定 | 必須ケース | 誤統合率 | 統合漏れ率 | "
        "処理時間（秒） | 失敗の内訳 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for model in GENERATION_MODELS:
        for row in sorted(reports.get(model, []), key=lambda row: (row["trial"], row["suite"])):
            cases = "; ".join(
                f"{identifier}={'PASS' if passed else 'FAIL'}"
                for identifier, passed in sorted(row["cases"].items())
            )
            failure = "; ".join(
                f"{identifier}: {reason}" for identifier, reason in sorted(row["failures"].items())
            )
            if row["completion"] != "complete":
                failure = row["reason"] + ("; " + failure if failure else "")
            metrics = row["metrics"]
            false_merge = "未測定" if metrics is None else _rate(metrics["false_merge_rate"])
            missed_merge = "未測定" if metrics is None else _rate(metrics["missed_merge_rate"])
            verdict = (
                "PASS" if row["completion"] == "complete" and all(row["cases"].values()) else "FAIL"
            )
            cells = [
                model,
                row["suite"],
                str(row["trial"]),
                verdict,
                cases,
                false_merge,
                missed_merge,
                str(row["elapsed_seconds"]),
                failure,
            ]
            lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    lines += ["", "誤統合率と統合漏れ率は記録用で、合否には使わない。", ""]
    if all(verdicts.get(model) == "FAIL" for model in GENERATION_MODELS):
        lines += ["両モデルとも不合格。採用の決定だけで PASS にしない。", ""]
    text = "\n".join(lines)
    validate_safe(text)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        target.write(text)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare public IT2 quality reports")
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        generate_comparison(args.report, args.output)
    except (ValueError, OSError):
        print("Comparison rejected; no safe new comparison was generated")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
