"""Environment settings shared by the service and ledger migrations."""

import json
import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    character_ids: frozenset[str] | None


def load_settings() -> Settings:
    database_url = os.environ["DATABASE_URL"]
    if not database_url.strip():
        raise ValueError("DATABASE_URL must not be empty")
    raw = os.environ.get("EVIDENCE_CHARACTER_IDS")
    character_ids: frozenset[str] | None = None
    if raw is not None:
        try:
            decoded: object = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list) and all(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in decoded
        ):
            character_ids = frozenset(decoded)
    return Settings(database_url=database_url, character_ids=character_ids)
