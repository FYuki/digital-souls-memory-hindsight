"""Environment settings shared by the service and ledger migrations."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str


def load_settings() -> Settings:
    database_url = os.environ["DATABASE_URL"]
    if not database_url.strip():
        raise ValueError("DATABASE_URL must not be empty")
    return Settings(database_url=database_url)
