"""All permitted and rejected deletion state transitions."""

import importlib
from datetime import timedelta

import pytest

from tests.ut.test_delivery_scope import NOW

pytestmark = pytest.mark.ut
STATUSES = ("accepted", "processing", "completed", "failed")


@pytest.mark.parametrize("previous", STATUSES)
@pytest.mark.parametrize("following", STATUSES)
def test_only_processing_can_finish_and_terminal_states_cannot_transition(
    previous: str, following: str
) -> None:
    domain = importlib.import_module("digital_souls_memory_hindsight.deletion.domain")
    ports = importlib.import_module("digital_souls_memory_hindsight.deletion.ports")
    request = domain.DeletionRequest(
        request_id="d",
        requested_by="synthetic-author",
        target_turn_ids=("t-1",),
        status=previous,
        cascade_result=None,
        error=None,
        requested_at=NOW,
        completed_at=NOW if previous == "completed" else None,
        user_id=None,
    )
    at = NOW + timedelta(seconds=5)
    if (previous, following) not in {
        ("accepted", "processing"),
        ("processing", "completed"),
        ("processing", "failed"),
    }:
        with pytest.raises(ports.DeletionTransitionRejected):
            domain.transition(request, following, at, error="synthetic failure")
        assert request.status == previous
        return
    result = domain.transition(
        request, following, at, error="synthetic failure" if following == "failed" else None
    )
    assert result.status == following
    assert result.request_id == request.request_id
    assert result.target_turn_ids == ("t-1",)
    assert result.requested_at == NOW
    assert result.completed_at == (at if following == "completed" else None)
    assert result.error == ("synthetic failure" if following == "failed" else None)
    assert request.status == previous
