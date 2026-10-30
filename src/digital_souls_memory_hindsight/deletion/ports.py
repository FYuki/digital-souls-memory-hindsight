"""Persistence boundary for deletion requests and retained blocking scopes."""

from typing import Protocol

from .domain import DeletionRequest, DeletionStatus
from .domain import DeletionTransitionRejected as DeletionTransitionRejected


class DeletionConflict(Exception):
    """The request identifier already belongs to another selection."""


class DeletionNotFound(Exception):
    """The deletion request does not exist."""


class DeletionTargetNotFound(Exception):
    """A selected turn does not exist."""


class DeletionStore(Protocol):
    def get(self, request_id: str) -> DeletionRequest | None: ...
    def add(
        self, request: DeletionRequest, blocks: frozenset[tuple[str, str]]
    ) -> DeletionRequest: ...
    def save_transition(
        self, previous_status: DeletionStatus, updated: DeletionRequest
    ) -> DeletionRequest: ...
    def blocked_sessions(self, character_id: str) -> frozenset[str]: ...
