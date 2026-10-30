"""Provenance fixes trust: repetition cannot turn an inference into ledger evidence."""

from typing import Final, Literal

Trust = Literal["evidence", "inferred"]
EVIDENCE: Final[Trust] = "evidence"
INFERRED: Final[Trust] = "inferred"
