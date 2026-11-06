"""Environment settings shared by the service and ledger migrations."""

import json
import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .character_state.domain import DEFAULT_INPUT_MAX_CHARS, DEFAULT_TTL_SECONDS
from .evidence.delivery import DEFAULT_DELIVERY_TIMEOUT_SECONDS

# 仮の既定値。epic 5 の IT2 で実モデルを使って決める。
DEFAULT_RECALL_MIN_RERANKER_SCORE = 0.05


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float


def _load_llm_settings() -> LLMSettings | None:
    names = (
        "MEMORY_LLM_BASE_URL",
        "MEMORY_LLM_MODEL",
        "MEMORY_LLM_API_KEY",
        "MEMORY_LLM_TIMEOUT_SECONDS",
    )
    raw = [os.environ.get(name) for name in names]
    if all(value is None for value in raw):
        return None
    values: list[str] = []
    for name, value in zip(names, raw, strict=True):
        if value is None or not value.strip():
            raise ValueError(f"{name} must be set and nonblank")
        values.append(value)
    base_url, model, api_key, timeout_text = values
    try:
        url = urlsplit(base_url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError()
    except ValueError:
        raise ValueError("MEMORY_LLM_BASE_URL must be an HTTP(S) URL") from None
    try:
        timeout = float(timeout_text)
    except ValueError:
        raise ValueError("MEMORY_LLM_TIMEOUT_SECONDS must be a positive finite number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MEMORY_LLM_TIMEOUT_SECONDS must be a positive finite number")
    return LLMSettings(base_url, model, api_key, timeout)


@dataclass(frozen=True)
class Settings:
    database_url: str
    character_ids: frozenset[str] | None
    delivery_timeout_seconds: int
    hindsight_url: str | None
    recall_min_reranker_score: float
    author_id: str | None
    author_token: str | None = field(repr=False)
    character_state_ttl_seconds: int
    character_state_input_max_chars: int
    llm: LLMSettings | None


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
    timeout = int(
        os.environ.get("EVIDENCE_DELIVERY_TIMEOUT_SECONDS", str(DEFAULT_DELIVERY_TIMEOUT_SECONDS))
    )
    if timeout <= 0:
        raise ValueError("EVIDENCE_DELIVERY_TIMEOUT_SECONDS must be positive")
    hindsight_url = os.environ.get("HINDSIGHT_URL")
    if hindsight_url is not None and not hindsight_url.strip():
        raise ValueError("HINDSIGHT_URL must not be empty")
    recall_min_reranker_score = float(
        os.environ.get("RECALL_MIN_RERANKER_SCORE", str(DEFAULT_RECALL_MIN_RERANKER_SCORE))
    )
    if not math.isfinite(recall_min_reranker_score) or not 0 <= recall_min_reranker_score <= 1:
        raise ValueError("RECALL_MIN_RERANKER_SCORE must be finite and between 0 and 1")
    author_id = os.environ.get("MEMORY_AUTHOR_ID")
    author_token = os.environ.get("MEMORY_AUTHOR_TOKEN")
    state_ttl = int(os.environ.get("CHARACTER_STATE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
    state_chars = int(
        os.environ.get("CHARACTER_STATE_INPUT_MAX_CHARS", str(DEFAULT_INPUT_MAX_CHARS))
    )
    if state_ttl <= 0 or state_chars <= 0:
        raise ValueError("Character state limits must be positive")
    return Settings(
        database_url=database_url,
        character_ids=character_ids,
        delivery_timeout_seconds=timeout,
        hindsight_url=hindsight_url,
        recall_min_reranker_score=recall_min_reranker_score,
        author_id=author_id if author_id is not None and author_id.strip() else None,
        author_token=author_token if author_token is not None and author_token.strip() else None,
        character_state_ttl_seconds=state_ttl,
        character_state_input_max_chars=state_chars,
        llm=_load_llm_settings(),
    )
