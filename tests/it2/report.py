"""Generate public JSON from explicit fields without collecting host diagnostics."""

import argparse
import json
import re
from pathlib import Path

from .scoring import SCORER_VERSION

ALLOWED_FIELDS = frozenset(
    {
        "run_id",
        "authorization",
        "environment",
        "revision",
        "timestamp",
        "models",
        "images",
        "settings",
        "scorer_version",
        "completion",
        "trials",
        "costs_and_constraints",
        "synthetic_inputs",
    }
)
_SECRET = re.compile(
    r"(?<![A-Za-z0-9_])(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+"
    r"|(?<![A-Za-z0-9_])Bearer\s+\S+"
    r"|(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|/(?:home|Users|root)/"
    r"|[A-Za-z]:\\+Users\\+",
    re.IGNORECASE,
)


def _contains_secret(value: object) -> bool:
    if isinstance(value, str):
        return _SECRET.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def generate_report(payload: dict[str, object], output_dir: Path) -> Path:
    if set(payload) != ALLOWED_FIELDS:
        raise ValueError("Report fields are missing or not approved")
    if payload["scorer_version"] != SCORER_VERSION:
        raise ValueError("Report scorer version does not match this revision")
    identifier = payload["run_id"]
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier
    ):
        raise ValueError("Invalid report identifier")
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if _SECRET.search(serialized) or _contains_secret(payload):
        raise ValueError("Report contains a credential-like value or personal home path")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{identifier}.json"
    with target.open("x", encoding="utf-8") as output:
        output.write(serialized)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a safe public IT2 report")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload: object = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise ValueError("Report input must be an object")
        generate_report(payload, args.output_dir)
    except (ValueError, OSError):
        # Neither external parser errors nor paths may expose rejected report values.
        print("Report rejected; no safe new report was generated")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
