"""The generation model is replaced by configuration only (A14): src names no model."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut
SRC = Path(__file__).resolve().parents[2] / "src"
MODEL_NAME = re.compile(r"gemma|gpt-\d|\bluna\b|bge-(?:m3|reranker)", re.IGNORECASE)


def test_src_files_exist() -> None:
    assert list(SRC.rglob("*.py"))


@pytest.mark.parametrize(
    "path", sorted(SRC.rglob("*")), ids=lambda path: str(path.relative_to(SRC))
)
def test_src_does_not_name_a_model(path: Path) -> None:
    if not path.is_file() or "__pycache__" in path.parts:
        return
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert MODEL_NAME.findall(text) == []
