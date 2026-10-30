"""Deletion selection identity and processing state transitions."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Literal

DeletionStatus = Literal["accepted", "processing", "completed", "failed"]


class DeletionTransitionRejected(Exception):
    """The requested transition is not allowed or its previous state has changed."""


@dataclass(frozen=True)
class DeletionRequest:
    request_id: str
    requested_by: str
    target_turn_ids: tuple[str, ...]
    status: DeletionStatus
    cascade_result: Mapping[str, object] | None
    error: str | None
    requested_at: datetime
    completed_at: datetime | None
    user_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_turn_ids", tuple(self.target_turn_ids))
        if self.cascade_result is not None:
            object.__setattr__(self, "cascade_result", MappingProxyType(dict(self.cascade_result)))


ALLOWED = frozenset(
    {("accepted", "processing"), ("processing", "completed"), ("processing", "failed")}
)


def transition(
    request: DeletionRequest,
    status: DeletionStatus,
    at: datetime,
    *,
    cascade_result: Mapping[str, object] | None = None,
    error: str | None = None,
) -> DeletionRequest:
    if (request.status, status) not in ALLOWED or (status == "failed" and not error):
        raise DeletionTransitionRejected()
    return replace(
        request,
        status=status,
        cascade_result=cascade_result,
        error=error if status == "failed" else None,
        completed_at=at if status == "completed" else None,
    )


def same_selection(
    request: DeletionRequest, target_turn_ids: tuple[str, ...] | None, user_id: str | None
) -> bool:
    if request.user_id is not None:
        return target_turn_ids is None and request.user_id == user_id
    return (
        user_id is None
        and target_turn_ids is not None
        and request.target_turn_ids == tuple(sorted(set(target_turn_ids)))
    )
