"""AC-I5 scenarios through real HTTP routes, with explicit boundary ports."""

import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, cast

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from digital_souls_memory_hindsight.consistency.ports import (
    ConsistencyReview,
    Reviewer,
    checked_review,
)
from digital_souls_memory_hindsight.identity.ports import LLMGateway, Temperament

from .consistency import (
    ConsistencyCase,
    consistency_character_id,
    score_case,
    score_step,
    summarize_consistency_trials,
)
from .quality_run import session_payload, turn_payload

DELETION_TIMEOUT_SECONDS = 1800.0


class RecordingGateway:
    def __init__(self, delegate: LLMGateway) -> None:
        self._delegate = delegate
        self.calls: list[dict[str, object]] = []

    def _record(self, generate: Callable[[], str], inputs: dict[str, object]) -> str:
        row: dict[str, object] = {"input": inputs, "output": None, "error": None}
        self.calls.append(row)
        try:
            output = generate()
            row["output"] = output
            return output
        except Exception as error:
            row["error"] = type(error).__name__
            raise

    def render_personality(
        self, aspects: dict[str, float], temperament: Temperament, renderer_version: str
    ) -> str:
        return self._record(
            lambda: self._delegate.render_personality(aspects, temperament, renderer_version),
            {
                "aspects": dict(aspects),
                "temperament": temperament.description,
                "renderer_version": renderer_version,
            },
        )

    def respond_in_voice(
        self, situation: str, answer: str, question: str | None, temperament: str | None
    ) -> str:
        return self._record(
            lambda: self._delegate.respond_in_voice(situation, answer, question, temperament),
            {
                "situation": situation,
                "answer": answer,
                "question": question,
                "temperament": temperament,
            },
        )


class RecordingReviewer:
    def __init__(self, delegate: Reviewer) -> None:
        self._delegate = delegate
        self.calls: list[dict[str, object]] = []

    def _record(
        self, review: Callable[[], ConsistencyReview], inputs: dict[str, object]
    ) -> ConsistencyReview:
        row: dict[str, object] = {"input": inputs, "review": None, "error": None}
        self.calls.append(row)
        try:
            result = checked_review(review())
            row["review"] = {"consistent": result.consistent, "reason": result.reason}
            return result
        except Exception as error:
            row["error"] = type(error).__name__
            raise

    def review_personality(self, temperament: str, personality: str) -> ConsistencyReview:
        return self._record(
            lambda: self._delegate.review_personality(temperament, personality),
            {"temperament": temperament, "personality": personality},
        )

    def review_voice(
        self,
        situation: str,
        response: str,
        answer: str,
        question: str | None,
        temperament: str | None,
    ) -> ConsistencyReview:
        return self._record(
            lambda: self._delegate.review_voice(situation, response, answer, question, temperament),
            {
                "situation": situation,
                "response": response,
                "answer": answer,
                "question": question,
                "temperament": temperament,
            },
        )


def _expected(
    *, consistent: bool | None, status: int | None, saved: bool, generation: int, reviews: int
) -> dict[str, object]:
    return {
        "consistent": consistent,
        "status": status,
        "saved": saved,
        "generation_calls": generation,
        "review_calls": reviews,
    }


def _observation(
    generation: Sequence[dict[str, object]], reviews: Sequence[dict[str, object]]
) -> dict[str, object]:
    return {
        "generation_calls": len(generation),
        "review_calls": len(reviews),
        "generated_text": generation[-1]["output"] if generation else None,
        "generation_error": next((c["error"] for c in generation if c["error"] is not None), None),
        "review_error": next((c["error"] for c in reviews if c["error"] is not None), None),
        "review": reviews[-1]["review"] if reviews else None,
        "generation": list(generation),
        "reviews": list(reviews),
        "status": None,
        "saved": False,
        "response_review": None,
        "response_review_present": True,
        "read_has_review": False,
    }


def run_reviewer_case(*, case: ConsistencyCase, reviewer: RecordingReviewer) -> dict[str, object]:
    inputs = case["input"]
    start = len(reviewer.calls)
    try:
        if case["operation"] == "personality":
            reviewer.review_personality(
                cast(str, inputs["temperament"]), cast(str, inputs["personality"])
            )
        else:
            reviewer.review_voice(
                cast(str, inputs["situation"]),
                cast(str, inputs["response"]),
                cast(str, inputs["answer"]),
                cast(str | None, inputs["question"]),
                cast(str | None, inputs["temperament"]),
            )
    except Exception:
        # The recorder already retained the stage and type; the body must not escape.
        pass
    observed = _observation([], reviewer.calls[start:])
    observed["response_review"] = observed["review"]
    expected = _expected(
        consistent=cast(bool, case["expected"]["consistent"]),
        status=None,
        saved=False,
        generation=0,
        reviews=1,
    )
    return score_case([score_step(expected=expected, observed=observed)])


class CaseExecutor(Protocol):
    def execute(self, *, case: ConsistencyCase, character_id: str) -> dict[str, object]: ...


class Recorder(Protocol):
    def record(self, row: Mapping[str, object]) -> None: ...


def run_consistency_trials(
    *, cases: list[ConsistencyCase], executor: CaseExecutor, recorder: Recorder
) -> dict[str, object]:
    trials: list[dict[str, object]] = []
    for trial in (1, 2, 3):
        results: list[dict[str, object]] = []
        for case in cases:
            character = consistency_character_id(case_id=case["id"], trial=trial)
            try:
                result = executor.execute(case=case, character_id=character)
            except Exception as error:
                result = {
                    "outcome": "execution_error",
                    "reason": f"execution_error:execute:{type(error).__name__}",
                    "steps": [],
                }
            results.append(
                {**result, "id": case["id"], "level": case["level"], "character_id": character}
            )
        row: dict[str, object] = {"trial": trial, "cases": results}
        recorder.record(row)
        trials.append(row)
    return summarize_consistency_trials(trials, required_case_ids={case["id"] for case in cases})


class ClientFactory(Protocol):
    def open(
        self, gateway: LLMGateway, reviewer: Reviewer
    ) -> AbstractContextManager[TestClient]: ...


class Lifecycle(Protocol):
    def review_row(self, character_id: str) -> dict[str, object] | None: ...
    def delete_source(self, client: TestClient, turn_id: str) -> None: ...


class PostgresConsistencyState:
    def __init__(self, database_url: str, author_token: str) -> None:
        self._database_url = database_url
        self._author_token = author_token

    def review_row(self, character_id: str) -> dict[str, object] | None:
        with psycopg.connect(self._database_url, connect_timeout=5, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT character_id, exemplar_id, consistent, reason, reviewed_at "
                "FROM voice_consistency_reviews WHERE character_id=%s",
                (character_id,),
            ).fetchone()
        if row is None:
            return None
        return {**row, "reviewed_at": row["reviewed_at"].isoformat()}

    def delete_source(self, client: TestClient, turn_id: str) -> None:
        headers = {"Authorization": "Bearer " + self._author_token}
        request_id = turn_id + "-delete"
        accepted = client.post(
            "/deletion-requests",
            headers=headers,
            json={"request_id": request_id, "target_turn_ids": [turn_id]},
        )
        if accepted.status_code != 202:
            raise ValueError("Synthetic source deletion was not accepted")
        deadline = time.monotonic() + DELETION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = client.get(f"/deletion-requests/{request_id}", headers=headers)
            if time.monotonic() >= deadline:
                break
            if response.status_code != 200:
                raise ValueError("Cannot read synthetic deletion status")
            status = response.json()["status"]
            if status == "completed":
                return
            if status not in {"accepted", "processing"}:
                raise ValueError("Synthetic source deletion failed")
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        raise TimeoutError("Synthetic source deletion wait exceeded")


def _contains_review(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"review", "consistency_review"} or _contains_review(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_review(item) for item in value)
    return False


class HttpConsistencyExecutor:
    def __init__(
        self, gateway: LLMGateway, reviewer: Reviewer, clients: ClientFactory, lifecycle: Lifecycle
    ) -> None:
        self._gateway = gateway
        self._reviewer = reviewer
        self._clients = clients
        self._lifecycle = lifecycle

    def execute(self, *, case: ConsistencyCase, character_id: str) -> dict[str, object]:
        gateway = RecordingGateway(self._gateway)
        reviewer = RecordingReviewer(self._reviewer)
        if case["level"] == "reviewer":
            return run_reviewer_case(case=case, reviewer=reviewer)
        scenario = _HttpScenario(character_id, gateway, reviewer, self._lifecycle)
        try:
            with self._clients.open(gateway, reviewer) as client:
                scenario.run(client, case)
            if case["operation"] == "voice_no_rereview" and scenario.ready_to_restart:
                with self._clients.open(gateway, reviewer) as restored:
                    scenario.voice(restored, case, "after_restart", review=False)
        except Exception as error:
            scenario.steps.append(
                {
                    "outcome": "execution_error",
                    "reason": f"execution_error:{scenario.stage}:{type(error).__name__}",
                }
            )
        return score_case(scenario.steps)


class _HttpScenario:
    def __init__(
        self,
        character: str,
        gateway: RecordingGateway,
        reviewer: RecordingReviewer,
        lifecycle: Lifecycle,
    ) -> None:
        self.character = character
        self.root = f"/characters/{character}"
        self.gateway = gateway
        self.reviewer = reviewer
        self.lifecycle = lifecycle
        self.steps: list[dict[str, object]] = []
        self.stage = "open_app"
        self.initial_review: dict[str, object] | None = None
        self.ready_to_restart = False

    def _register(
        self,
        client: TestClient,
        path: str,
        body: dict[str, object],
        expected: dict[str, object],
        *,
        review_key: str,
        version_path: str | None,
    ) -> bool:
        generation, reviews = len(self.gateway.calls), len(self.reviewer.calls)
        before = client.get(self.root + "/identity/current")
        if before.status_code != 200:
            raise ValueError("Cannot read current identity")
        response = client.post(self.root + path, json=body)
        after = client.get(self.root + "/identity/current")
        if after.status_code != 200:
            raise ValueError("Cannot read current identity after registration")
        read = (
            client.get(self.root + version_path)
            if version_path
            else client.get(self.root + "/voice-exemplars")
        )
        if read.status_code not in {200, 404}:
            raise ValueError("Cannot inspect saved registration")
        saved_body = read.json()
        saved = read.status_code == 200 and (
            version_path is not None
            or any(item["exemplar_id"] == body["exemplar_id"] for item in saved_body)
        )
        observed = _observation(self.gateway.calls[generation:], self.reviewer.calls[reviews:])
        response_body = response.json()
        observed.update(
            stage=self.stage,
            status=response.status_code,
            saved=saved,
            response_review=response_body.get(review_key),
            response_review_present=review_key in response_body,
            read_has_review=_contains_review(saved_body) or _contains_review(after.json()),
            current_before=before.json(),
            current_after=after.json(),
            candidate_version_status=read.status_code,
            saved_version=saved_body.get("version") if version_path else None,
            total_generation_calls=len(self.gateway.calls),
            total_review_calls=len(self.reviewer.calls),
        )
        if "review_row_unchanged" in expected:
            observed.update(
                review_row_before=self.initial_review,
                review_row_after=self.lifecycle.review_row(self.character),
            )
        scored = score_step(expected=expected, observed=observed)
        self.steps.append(scored)
        return scored["outcome"] == "correct"

    def seed(self, client: TestClient, case: ConsistencyCase, consistent: bool) -> bool:
        self.stage = "author_seed"
        expected = _expected(
            consistent=consistent,
            status=201 if consistent else 422,
            saved=consistent,
            generation=1,
            reviews=1,
        )
        expected["candidate_version_status"] = 200 if consistent else 404
        if consistent:
            expected["saved_version"] = 1
        else:
            expected["current_unchanged"] = True
        return self._register(
            client,
            "/personality-profiles/author-seed",
            cast(dict[str, object], case["input"]["seed"]),
            expected,
            review_key="review" if consistent else "consistency_review",
            version_path="/personality-profiles/1",
        )

    def voice(self, client: TestClient, case: ConsistencyCase, stage: str, *, review: bool) -> bool:
        self.stage = stage
        body = dict(cast(dict[str, object], case["input"]["voice"]))
        body["exemplar_id"] = stage
        if stage == "first_turn":
            body.update(
                source="turn",
                answer_span={
                    "turn_id": self.character + "-turn",
                    "start": 0,
                    "end": len(cast(str, body["answer"])),
                },
            )
        consistent = cast(bool, case["expected"]["consistent"]) if review else None
        expected = _expected(
            consistent=consistent,
            status=201,
            saved=True,
            generation=int(review),
            reviews=int(review),
        )
        if not review:
            expected.update(
                review_row_unchanged=True, total_generation_calls=1, total_review_calls=1
            )
        return self._register(
            client,
            "/voice-exemplars",
            body,
            expected,
            review_key="consistency_review",
            version_path=None,
        )

    def run(self, client: TestClient, case: ConsistencyCase) -> None:
        self.stage = "initial_temperament"
        if not self._register(
            client,
            "/temperaments",
            {"description": case["input"]["temperament"]},
            {
                **_expected(consistent=None, status=201, saved=True, generation=0, reviews=0),
                "saved_version": 1,
            },
            review_key="review",
            version_path="/temperaments/1",
        ):
            return
        consistent = cast(bool, case["expected"]["consistent"])
        if case["operation"] == "seed":
            self.seed(client, case, consistent)
        elif case["operation"] == "temperament":
            if not self.seed(client, case, True):
                return
            self.stage = "updated_temperament"
            expected = _expected(
                consistent=consistent,
                status=201 if consistent else 422,
                saved=consistent,
                generation=0,
                reviews=1,
            )
            expected["candidate_version_status"] = 200 if consistent else 404
            if consistent:
                expected["saved_version"] = 2
            else:
                expected["current_unchanged"] = True
            self._register(
                client,
                "/temperaments",
                {"description": case["input"]["update"]},
                expected,
                review_key="review" if consistent else "consistency_review",
                version_path="/temperaments/2",
            )
        elif case["operation"] == "voice_first":
            self.voice(client, case, "first", review=True)
        else:
            self._voice_lifecycle(client, case)

    def _voice_lifecycle(self, client: TestClient, case: ConsistencyCase) -> None:
        self.stage = "source_evidence"
        session = self.character + "-session"
        turn = self.character + "-turn"
        body = cast(dict[str, object], case["input"]["voice"])
        if client.post("/sessions", json=session_payload(session)).status_code != 201:
            raise ValueError("Cannot create synthetic session")
        if (
            client.post(
                "/turns",
                json=turn_payload(
                    character_id=self.character,
                    session_id=session,
                    turn_id=turn,
                    text=cast(str, body["answer"]),
                    index=0,
                ),
            ).status_code
            != 201
        ):
            raise ValueError("Cannot create synthetic turn")
        if not self.voice(client, case, "first_turn", review=True):
            return
        self.stage = "initial_review_row"
        self.initial_review = self.lifecycle.review_row(self.character)
        if self.initial_review is None:
            raise ValueError("Initial voice review was not saved")
        self.stage = "delete_source"
        generation, reviews = len(self.gateway.calls), len(self.reviewer.calls)
        self.lifecycle.delete_source(client, turn)
        remaining = client.get(self.root + "/voice-exemplars")
        observed = _observation(self.gateway.calls[generation:], self.reviewer.calls[reviews:])
        observed.update(
            stage="after_source_deletion",
            status=remaining.status_code,
            saved=remaining.json() != [],
            review_row_before=self.initial_review,
            review_row_after=self.lifecycle.review_row(self.character),
            total_generation_calls=len(self.gateway.calls),
            total_review_calls=len(self.reviewer.calls),
        )
        expected = {
            **_expected(consistent=None, status=200, saved=False, generation=0, reviews=0),
            "review_row_unchanged": True,
            "total_generation_calls": 1,
            "total_review_calls": 1,
        }
        scored = score_step(expected=expected, observed=observed)
        self.steps.append(scored)
        if scored["outcome"] != "correct":
            return
        if not self.voice(client, case, "after_deletion", review=False):
            return
        if not self.voice(client, case, "subsequent", review=False):
            return
        self.ready_to_restart = True
