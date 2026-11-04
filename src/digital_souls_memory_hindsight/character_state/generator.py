"""One claimed generation produces at most one validated state."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..evidence.ports import EvidenceNotFound
from ..evidence.service import EvidenceService
from .domain import (
    DEFAULT_INPUT_MAX_CHARS,
    InvalidStateOutput,
    StateInput,
    input_window,
    select_input,
    validate_output,
)
from .ports import CharacterStateStore, ContextGateway, StateDeletion, StateJob


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CharacterStateGenerator:
    def __init__(
        self,
        store: CharacterStateStore,
        evidence: EvidenceService,
        deletion: StateDeletion,
        gateway: ContextGateway,
        *,
        clock: Callable[[], datetime] = _utc_now,
        input_max_chars: int = DEFAULT_INPUT_MAX_CHARS,
    ) -> None:
        if input_max_chars <= 0:
            raise ValueError("State input limit must be positive")
        self._store = store
        self._evidence = evidence
        self._deletion = deletion
        self._gateway = gateway
        self._clock = clock
        self._input_max_chars = input_max_chars

    def _prepare(self, job: StateJob) -> tuple[StateInput, ...]:
        try:
            session = self._evidence.get_session(job.key.session_id)
        except EvidenceNotFound:
            return ()
        if session.ended_at is not None:
            return ()
        sources = self._evidence.state_input_sources(*job.key, 20)
        eligible = (
            (turn, text)
            for turn, text in sources
            if not self._deletion.turns_under_deletion(frozenset({turn.turn_id}))
        )
        window = input_window(
            eligible,
            max_turns=20,
            max_chars=self._input_max_chars,
        )
        return select_input(window, max_chars=self._input_max_chars)

    async def run_once(self) -> None:
        job = await asyncio.to_thread(self._store.claim, self._clock(), timedelta(seconds=60))
        if job is None:
            return
        turns = await asyncio.to_thread(self._prepare, job)
        if not turns:
            await asyncio.to_thread(self._store.complete, job, self._clock())
            return
        try:
            raw = await asyncio.wait_for(self._gateway.generate_state(*job.key, turns), timeout=30)
        except Exception:
            now = self._clock()
            if job.attempts < 3:
                await asyncio.to_thread(
                    self._store.defer,
                    job,
                    now,
                    now + timedelta(seconds=2 ** (job.attempts - 1)),
                    "gateway_unavailable",
                )
            else:
                await asyncio.to_thread(self._store.fail, job, now, "gateway_unavailable")
            return
        try:
            state = validate_output(raw)
        except InvalidStateOutput:
            await asyncio.to_thread(self._store.fail, job, self._clock(), "invalid_output")
            return
        await asyncio.to_thread(self._store.save, job, state, self._clock())
