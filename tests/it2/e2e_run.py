"""AC-I4 HTTP scenario, read-only inspection, and fixed independent trials."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import NotRequired, Protocol, TypedDict, cast

import httpx
import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from digital_souls_memory_hindsight.character_state.domain import (
    InvalidStateOutput,
    validate_output,
)
from digital_souls_memory_hindsight.evidence.projection import bank_id, document_id
from digital_souls_memory_hindsight.evidence_http import ReceiptRequest, SessionRequest, TurnRequest
from digital_souls_memory_hindsight.voice_http import VoiceRequest

from .consistency_run import DELETION_TIMEOUT_SECONDS
from .e2e import (
    STEPS,
    E2ECase,
    compare_resident_provision,
    compare_state_update,
    compare_turn_provision,
    deletion_residuals,
    e2e_character_id,
)
from .quality_run import CompletionPort, JsonlTrialRecorder, MemoryReader
from .report import validate_safe
from .scoring import TrialResult, summarize_trials

STATE_WAIT_SECONDS = 600.0


class RequestObservation(TypedDict):
    step: str
    method: str
    path: str
    status: int
    input: dict[str, object] | None
    params: dict[str, str] | None


class E2ETrial(TrialResult):
    character_id: str
    reason: str
    failures: dict[str, str]
    observations: dict[str, object]
    requests: list[RequestObservation]
    elapsed_seconds: float
    trial: NotRequired[int]


class E2ERun(TypedDict):
    verdict: str
    trials: list[E2ETrial]


class Inspection(CompletionPort, MemoryReader, Protocol):
    pass


class StateReader(Protocol):
    def provisions(self, character: str) -> list[dict[str, object]]: ...
    def states(self, character: str) -> list[dict[str, object]]: ...
    def outbox_count(self, bank: str) -> int: ...


class PostgresE2EState:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def _rows(self, sql: str, key: str) -> list[dict[str, object]]:
        with psycopg.connect(
            self._url,
            connect_timeout=5,
            options="-c default_transaction_read_only=on",
            row_factory=dict_row,
        ) as connection:
            return cast(list[dict[str, object]], connection.execute(sql, (key,)).fetchall())

    def provisions(self, character: str) -> list[dict[str, object]]:
        return self._rows(
            "SELECT provision_id,kind,character_id,session_id,surface,core_identity_version,"
            "temperament_version,personality_version,exemplar_ids,profile_revision,profile_entries,"
            "memory_ids,source_spans,verbatim_anchor_ids,memory_trust,source_span_trust,"
            "verbatim_anchor_trust,resident_provision_id FROM provision_records "
            "WHERE character_id=%s ORDER BY created_at,provision_id",
            character,
        )

    def states(self, character: str) -> list[dict[str, object]]:
        rows = self._rows(
            "SELECT s.session_id,s.surface,s.generation,s.state_generation,s.generated_at,"
            "s.mood,s.relationship_temperature,s.current_topic,o.status,o.attempts,o.last_error "
            "FROM character_states s LEFT JOIN character_state_outbox o "
            "USING (character_id,session_id,surface) WHERE s.character_id=%s "
            "ORDER BY s.session_id,s.surface",
            character,
        )
        for row in rows:
            stamp = cast(datetime | None, row["generated_at"])
            row["generated_at"] = None if stamp is None else stamp.isoformat()
            row["state"] = (
                None
                if row["mood"] is None
                else {
                    key: row[key] for key in ("mood", "relationship_temperature", "current_topic")
                }
            )
            for key in ("mood", "relationship_temperature", "current_topic"):
                del row[key]
        return rows

    def outbox_count(self, bank: str) -> int:
        return cast(
            int,
            self._rows("SELECT count(*) AS count FROM outbox WHERE bank_id=%s", bank)[0]["count"],
        )


def session_request(identifier: str, surface: str) -> dict[str, object]:
    return SessionRequest.model_validate(
        {
            "session_id": identifier,
            "surface": surface,
            "reports_delivery": True,
            "started_at": "2026-11-05T00:00:00Z",
        }
    ).model_dump(mode="json")


def turn_request(
    *,
    character: str,
    identifier: str,
    session: str,
    surface: str,
    text: str,
    speaking: bool,
) -> dict[str, object]:
    return TurnRequest.model_validate(
        {
            "turn_id": identifier,
            "session_id": session,
            "surface": surface,
            "actor": character if speaking else "synthetic-user",
            "kind": "utterance",
            "text": text,
            "perceived_by": [character],
            "addressees": [],
            "address_mode": "broadcast",
            "language": "ja",
            "register": {},
            "sensitivity": "public",
            "classifier_version": "it2-e2e-v1",
            "occurred_at": "2026-11-05T00:01:00Z",
        }
    ).model_dump(mode="json", by_alias=True)


class _Stop(Exception):
    pass


class _Scenario:
    def __init__(
        self,
        *,
        case: E2ECase,
        character: str,
        client: httpx.Client | TestClient,
        state: StateReader,
        inspection: Inspection,
        author_token: str,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.case, self.character, self.client = case, character, client
        self.state, self.inspection = state, inspection
        self._token, self.clock, self.sleep = author_token, clock, sleep
        self.root = f"/characters/{character}"
        self.main, self.source, self.other, self.empty = (
            f"{character}-session-{name}" for name in ("main", "source", "other", "empty")
        )
        self.bank = bank_id(character)
        self.turns: dict[str, dict[str, object]] = {}
        self.residents: list[dict[str, object]] = []
        self.recall: dict[str, object] = {}
        self.state_before_delete: list[dict[str, object]] = []
        self.current = "prerequisites"
        self.completion = "failed"
        self.requests: list[RequestObservation] = []
        self.observed: dict[str, object] = {}
        self._reported_at = datetime.now(UTC)
        self._receipts = 0

    def call(
        self,
        method: str,
        path: str,
        *,
        expected: int,
        payload: dict[str, object] | None = None,
        author: bool = False,
        params: dict[str, str] | None = None,
    ) -> object:
        response = self.client.request(
            method,
            path,
            json=payload,
            params=params,
            headers={"Authorization": f"Bearer {self._token}"} if author else {},
        )
        self.requests.append(
            {
                "step": self.current,
                "method": method,
                "path": path,
                "status": response.status_code,
                "input": payload,
                "params": params,
            }
        )
        if response.status_code != expected:
            raise _Stop(f"http_status:{self.current}:{method}:{path}:{response.status_code}")
        return cast(object, response.json())

    def obj(
        self,
        method: str,
        path: str,
        *,
        expected: int = 200,
        payload: dict[str, object] | None = None,
        author: bool = False,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.call(
                method,
                path,
                expected=expected,
                payload=payload,
                author=author,
                params=params,
            ),
        )

    def add_session(self, identifier: str, surface: str) -> None:
        self.obj("POST", "/sessions", expected=201, payload=session_request(identifier, surface))

    def add_turn(self, suffix: str, session: str, surface: str, text: str, speaking: bool) -> None:
        identifier = f"{self.character}-turn-{suffix}"
        body = turn_request(
            character=self.character,
            identifier=identifier,
            session=session,
            surface=surface,
            text=text,
            speaking=speaking,
        )
        self.turns[suffix] = self.obj("POST", "/turns", expected=201, payload=body)

    def receipt(self, suffix: str) -> None:
        turn = self.turns[suffix]
        self._receipts += 1
        self.obj(
            "POST",
            f"/turns/{turn['turn_id']}/receipts",
            expected=201,
            payload=ReceiptRequest.model_validate(
                {
                    "status": "full",
                    "spoken_until": len(cast(str, turn["text"])),
                    "reported_by": "synthetic-core",
                    "reported_at": (
                        self._reported_at + timedelta(microseconds=self._receipts)
                    ).isoformat(),
                }
            ).model_dump(mode="json"),
        )

    def profile(self, suffixes: tuple[str, ...]) -> dict[str, object]:
        return self.obj(
            "PUT",
            self.root + "/profile",
            payload={
                "char_limit": 2000,
                "entries": [
                    {
                        "turn_id": self.turns[key]["turn_id"],
                        "start": 0,
                        "end": len(cast(str, self.turns[key]["text"])),
                    }
                    for key in suffixes
                ],
            },
        )

    def voice(self, index: int) -> dict[str, object]:
        return self.obj(
            "POST",
            self.root + "/voice-exemplars",
            expected=201,
            payload=VoiceRequest.model_validate(
                {
                    "exemplar_id": f"{self.character}-voice-{index + 1}",
                    "source": "canonical_seed",
                    "answer": self.case.voice_answers[index],
                    "approved_by": "synthetic-author",
                }
            ).model_dump(mode="json"),
        )

    def record(self, identifier: object) -> dict[str, object] | None:
        return next(
            (r for r in self.state.provisions(self.character) if r["provision_id"] == identifier),
            None,
        )

    def resident(self) -> dict[str, object]:
        return self.obj(
            "POST",
            self.root + "/resident",
            payload={"session_id": self.main, "surface": "surface-a"},
        )

    def resident_checks(self, response: dict[str, object], version: int) -> list[str]:
        record = self.record(response["provision_id"])
        mismatches = (
            ["provision_missing"]
            if record is None
            else compare_resident_provision(
                response=response,
                record=record,
            )
        )
        for key, expected in (
            ("core_identity", version),
            ("temperament", version),
            ("personality_profile", 1),
        ):
            value = cast(dict[str, object] | None, response[key])
            if value is None or value["version"] != expected:
                mismatches.append(key)
        for key, expected_fields in (
            ("core_identity", self.case.cores[version - 1].model_dump()),
            ("temperament", self.case.temperaments[version - 1].model_dump()),
        ):
            selected = cast(dict[str, object] | None, response[key])
            if selected is None or any(
                selected[field] != value for field, value in expected_fields.items()
            ):
                mismatches.append(key + "_content")
        if record is not None and any(
            record[key] != value
            for key, value in (
                ("character_id", self.character),
                ("session_id", self.main),
                ("surface", "surface-a"),
            )
        ):
            mismatches.append("scope")
        profile = cast(dict[str, object] | None, response["character_profile"])
        if profile is None or profile["revision"] != version:
            mismatches.append("character_profile")
        voices = cast(list[dict[str, object]], response["voice_exemplars"])
        if {v["exemplar_id"] for v in voices} != {
            f"{self.character}-voice-{i}" for i in range(1, version + 1)
        }:
            mismatches.append("voice_exemplars")
        expected_voices = {
            f"{self.character}-voice-{i + 1}": answer
            for i, answer in enumerate(self.case.voice_answers[:version])
        }
        if any(v["answer"] != expected_voices.get(cast(str, v["exemplar_id"])) for v in voices):
            mismatches.append("voice_content")
        entries = [] if profile is None else cast(list[dict[str, object]], profile["entries"])
        expected_sources = ("source",) if version == 1 else ("source", "main")
        if entries != [
            {
                "turn_id": self.turns[s]["turn_id"],
                "start": 0,
                "end": len(cast(str, self.turns[s]["text"])),
                "text": self.turns[s]["text"],
            }
            for s in expected_sources
        ]:
            mismatches.append("profile_entries")
        self.observed[self.current] = {"response": response, "record": record}
        return mismatches

    def wait(self) -> None:
        result = self.inspection.wait(self.bank)
        self.completion = result.status
        self.observed[self.current + "_completion"] = {
            "status": result.status,
            "reason": result.reason,
        }
        if result.status != "complete":
            raise _Stop(result.reason)

    def prerequisites(self) -> list[str]:
        self.obj(
            "POST",
            self.root + "/core-identities",
            expected=201,
            payload=self.case.cores[0].model_dump(),
        )
        self.obj(
            "POST",
            self.root + "/temperaments",
            expected=201,
            payload=self.case.temperaments[0].model_dump(),
        )
        seed = self.obj(
            "POST",
            self.root + "/personality-profiles/author-seed",
            expected=201,
            payload=self.case.seed.model_dump(),
        )
        voice = self.voice(0)
        self.add_session(self.source, "surface-a")
        self.add_turn("source", self.source, "surface-a", self.case.source_text, False)
        profile = self.profile(("source",))
        self.observed[self.current] = {"seed": seed, "voice": voice, "profile": profile}
        return [] if seed["version"] == 1 and profile["revision"] == 1 else ["initial_versions"]

    def session_turn_receipt(self) -> list[str]:
        self.add_session(self.main, "surface-a")
        self.add_turn("main", self.main, "surface-a", self.case.main_text, False)
        self.add_turn("reply", self.main, "surface-a", self.case.reply_text, True)
        self.receipt("reply")
        self.add_turn("surface", self.main, "surface-b", self.case.surface_text, False)
        self.add_session(self.other, "surface-a")
        self.add_turn("other", self.other, "surface-a", self.case.other_text, False)
        self.add_session(self.empty, "surface-a")
        mismatches = []
        for key, turn in self.turns.items():
            saved = self.obj("GET", f"/turns/{turn['turn_id']}")
            sent = next(
                request["input"]
                for request in self.requests
                if request["method"] == "POST"
                and request["path"] == "/turns"
                and request["input"] is not None
                and request["input"]["turn_id"] == f"{self.character}-turn-{key}"
            )
            if (
                saved != turn
                or sent is None
                or any(saved[field] != value for field, value in sent.items())
            ):
                mismatches.append(f"turn:{key}")
        session = self.obj("GET", f"/sessions/{self.main}")
        receipts = self.call(
            "GET", f"/turns/{self.turns['reply']['turn_id']}/receipts", expected=200
        )
        receipt_path = f"/turns/{self.turns['reply']['turn_id']}/receipts"
        sent_receipt = next(
            request["input"]
            for request in self.requests
            if request["method"] == "POST" and request["path"] == receipt_path
        )
        if session != session_request(self.main, "surface-a") or {
            **cast(dict[str, object], sent_receipt),
            "turn_id": self.turns["reply"]["turn_id"],
        } not in cast(list[dict[str, object]], receipts):
            mismatches.append("session_or_receipt")
        self.observed[self.current] = {
            "session": session,
            "turns": self.turns.copy(),
            "receipts": receipts,
        }
        return mismatches

    def resident_first(self) -> list[str]:
        response = self.resident()
        self.residents.append(response)
        return self.resident_checks(response, 1)

    def resident_after_change(self) -> list[str]:
        self.wait()
        self.observed["initial_states"] = self.state.states(self.character)
        self.obj(
            "POST",
            self.root + "/core-identities",
            expected=201,
            payload=self.case.cores[1].model_dump(),
        )
        self.obj(
            "POST",
            self.root + "/temperaments",
            expected=201,
            payload=self.case.temperaments[1].model_dump(),
        )
        self.voice(1)
        self.profile(("source", "main"))
        response = self.resident()
        self.residents.append(response)
        mismatches = self.resident_checks(response, 2)
        first = self.record(self.residents[0]["provision_id"])
        initial = cast(dict[str, object], self.observed["resident_first"])
        if first != initial["record"]:
            mismatches.append("first_record_changed")
        return mismatches

    def turn_recall(self) -> list[str]:
        self.wait()
        response = self.obj(
            "POST",
            self.root + "/recall",
            payload={
                "session_id": self.main,
                "format": "materials",
                "turn_id": self.turns["main"]["turn_id"],
                "resident_provision_id": self.residents[1]["provision_id"],
            },
        )
        self.recall = response
        record = self.record(response["provision_id"])
        resident = self.record(self.residents[1]["provision_id"])
        mismatches = (
            ["provision_missing"]
            if record is None
            else compare_turn_provision(
                response=response,
                record=record,
                resident=resident,
            )
        )
        memories = cast(list[dict[str, object]], response["memories"])
        spans = cast(list[dict[str, object]], response["source_spans"])
        main_ids = {self.turns[k]["turn_id"] for k in ("main", "reply", "surface")}
        if not memories or not any(
            main_ids.intersection(cast(list[str], m["source_turn_ids"])) for m in memories
        ):
            mismatches.append("memories")
        if not spans or not any(s["turn_id"] in main_ids for s in spans):
            mismatches.append("source_spans")
        for span in spans:
            source = self.obj("GET", f"/turns/{span['turn_id']}")
            if (
                span["text"]
                != cast(str, source["text"])[cast(int, span["start"]) : cast(int, span["end"])]
            ):
                mismatches.append("source_text")
        self.observed[self.current] = {"response": response, "record": record, "resident": resident}
        return mismatches

    def state_response(self, session: str, surface: str) -> dict[str, object]:
        return self.obj(
            "GET", self.root + f"/sessions/{session}/state", params={"surface": surface}
        )

    def state_row(self, session: str, surface: str) -> dict[str, object] | None:
        return next(
            (
                row
                for row in self.state.states(self.character)
                if row["session_id"] == session and row["surface"] == surface
            ),
            None,
        )

    def regenerate(self, suffix: str) -> tuple[dict[str, object] | None, str]:
        turn = self.turns[suffix]
        session, surface = cast(str, turn["session_id"]), cast(str, turn["surface"])
        before = self.state_row(session, surface)
        generation = 0 if before is None else cast(int, before["generation"])
        self.receipt(suffix)
        deadline = self.clock() + STATE_WAIT_SECONDS
        while True:
            row = self.state_row(session, surface)
            response = self.state_response(session, surface)
            remaining = deadline - self.clock()
            if row is not None and cast(int, row["generation"]) > generation:
                if row["status"] == "failed":
                    return row, f"failed:{row['last_error']}"
                if (
                    remaining >= 0
                    and row["generation"] == row["state_generation"]
                    and row["state"] is not None
                    and response["state"] == row["state"]
                ):
                    try:
                        validate_output(response["state"])
                    except InvalidStateOutput:
                        return row, "failed:invalid_output"
                    return row, "generated"
            if remaining <= 0:
                return row, "timeout"
            self.sleep(min(1.0, remaining))

    def character_state(self) -> list[str]:
        mismatches = []
        count = self.state.outbox_count(self.bank)
        generated: dict[str, object] = {}
        for suffix in ("other", "surface", "reply"):
            row, reason = self.regenerate(suffix)
            generated[suffix] = {"row": row, "reason": reason}
            if reason != "generated":
                mismatches.append(f"{suffix}:{reason}")
        controls_before = [
            self.state_row(self.other, "surface-a"),
            self.state_row(self.main, "surface-b"),
        ]
        http_before = [
            self.state_response(self.other, "surface-a"),
            self.state_response(self.main, "surface-b"),
        ]
        before = self.state_row(self.main, "surface-a")
        after, reason = self.regenerate("reply")
        generated["reply_update"] = {"row": after, "reason": reason}
        controls_after = [
            self.state_row(self.other, "surface-a"),
            self.state_row(self.main, "surface-b"),
        ]
        http_after = [
            self.state_response(self.other, "surface-a"),
            self.state_response(self.main, "surface-b"),
        ]
        if reason != "generated" or before is None or after is None:
            mismatches.append(f"update:{reason}")
        else:
            mismatches.extend(
                compare_state_update(
                    before=before,
                    after=after,
                    others_before=cast(list[Mapping[str, object]], controls_before),
                    others_after=cast(list[Mapping[str, object]], controls_after),
                )
            )
        if http_before != http_after or any(r["state"] is None for r in http_after):
            mismatches.append("isolation")
        for session, surface in ((self.empty, "surface-a"), (self.main, "unused")):
            if (
                self.state_response(session, surface) != {"state": None}
                or self.state_row(session, surface) is not None
            ):
                mismatches.append("empty_scope")
        if self.state.outbox_count(self.bank) != count:
            mismatches.append("projection_outbox_changed")
        self.state_before_delete = self.state.states(self.character)
        self.observed[self.current] = {
            "generations": generated,
            "controls_before": controls_before,
            "controls_after": controls_after,
            "http_before": http_before,
            "http_after": http_after,
            "outbox_before": count,
            "outbox_after": self.state.outbox_count(self.bank),
        }
        return mismatches

    def author_deletion(self) -> list[str]:
        memories = self.inspection.read(self.bank)
        document = document_id(self.main)
        deleted_memories = {row["id"] for row in memories if row.get("document_id") == document}
        targets = {cast(str, self.turns[k]["turn_id"]) for k in ("main", "reply", "surface")}
        request = f"{self.character}-delete-main"
        self.obj(
            "POST",
            "/deletion-requests",
            expected=202,
            author=True,
            payload={"request_id": request, "target_turn_ids": sorted(targets)},
        )
        deadline = self.clock() + DELETION_TIMEOUT_SECONDS
        while True:
            deletion = self.obj("GET", f"/deletion-requests/{request}", author=True)
            remaining = deadline - self.clock()
            if deletion["status"] == "failed":
                raise _Stop("deletion_failed")
            if remaining < 0 or (remaining == 0 and deletion["status"] != "completed"):
                raise _Stop("deletion_timeout")
            if deletion["status"] == "completed":
                break
            self.sleep(min(1.0, remaining))
        mismatches = []
        for target in sorted(targets):
            self.call("GET", f"/turns/{target}", expected=404)
            self.call("GET", f"/turns/{target}/receipts", expected=404)
            self.call(
                "POST",
                self.root + "/recall",
                expected=404,
                payload={
                    "session_id": self.main,
                    "format": "materials",
                    "turn_id": target,
                },
            )
        turns = cast(list[dict[str, object]], self.call("GET", self.root + "/turns", expected=200))
        if targets.intersection(cast(str, row["turn_id"]) for row in turns):
            mismatches.append("turn_list")
        recalled = self.obj(
            "POST",
            self.root + "/recall",
            payload={
                "session_id": self.main,
                "format": "materials",
                "query": self.case.main_text,
                "sensitivity": "public",
                "classifier_version": "it2-e2e-v1",
            },
        )
        resident = self.resident()
        for response in (recalled, resident):
            if self.case.deletion_marker in json_texts(response):
                mismatches.append("response_text")
        after_memories = self.inspection.read(self.bank)
        after_provisions = self.state.provisions(self.character)
        mismatches.extend(
            deletion_residuals(
                memories=after_memories,
                provisions=after_provisions,
                deleted_turn_ids=targets,
                deleted_document_id=document,
                deleted_memory_ids=deleted_memories,
                marker=self.case.deletion_marker,
            )
        )
        saved_ids = {row["provision_id"] for row in after_provisions}
        if (
            self.recall["provision_id"] in saved_ids
            or self.residents[1]["provision_id"] in saved_ids
        ):
            mismatches.append("deleted_provision")
        if self.residents[0]["provision_id"] not in saved_ids:
            mismatches.append("control_provision")
        source = self.obj("GET", f"/turns/{self.turns['source']['turn_id']}")
        if source != self.turns["source"]:
            mismatches.append("control_turn")
        for surface in ("surface-a", "surface-b"):
            row = self.state_row(self.main, surface)
            before = next(
                r
                for r in self.state_before_delete
                if r["session_id"] == self.main and r["surface"] == surface
            )
            if (
                self.state_response(self.main, surface) != {"state": None}
                or row is None
                or cast(int, row["generation"]) <= cast(int, before["generation"])
            ):
                mismatches.append("deleted_state")
        if self.state_response(self.other, "surface-a")["state"] is None:
            mismatches.append("control_state")
        self.observed[self.current] = {
            "deletion": deletion,
            "deleted_memory_ids": sorted(deleted_memories),
            "memories": after_memories,
            "provisions": after_provisions,
            "recall": recalled,
            "resident": resident,
            "states": self.state.states(self.character),
        }
        return mismatches


def json_texts(value: object) -> str:
    if isinstance(value, dict):
        return "\n".join(json_texts(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(json_texts(v) for v in value)
    return value if isinstance(value, str) else ""


def run_e2e_trial(
    *,
    case: E2ECase,
    character_id: str,
    client: httpx.Client | TestClient,
    state: StateReader,
    inspection: Inspection,
    author_token: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> E2ETrial:
    scenario = _Scenario(
        case=case,
        character=character_id,
        client=client,
        state=state,
        inspection=inspection,
        author_token=author_token,
        clock=clock,
        sleep=sleep,
    )
    started = clock()
    cases = dict.fromkeys(STEPS, False)
    failures = dict.fromkeys(STEPS, "not_reached")
    for step, operation in (
        ("prerequisites", scenario.prerequisites),
        ("session_turn_receipt", scenario.session_turn_receipt),
        ("resident_first", scenario.resident_first),
        ("resident_after_change", scenario.resident_after_change),
        ("turn_recall", scenario.turn_recall),
        ("character_state", scenario.character_state),
        ("author_deletion", scenario.author_deletion),
    ):
        scenario.current = step
        try:
            mismatches = operation()
        except _Stop as stop:
            failures[step] = str(stop)
            break
        except Exception as error:
            failures[step] = f"execution_error:{step}:{type(error).__name__}"
            break
        cases[step] = not mismatches
        failures[step] = ",".join(mismatches) if mismatches else "matched"
    reason = next((failures[step] for step in STEPS if not cases[step]), "matched")
    result: E2ETrial = {
        "bank_id": scenario.bank,
        "character_id": character_id,
        "completion": scenario.completion,
        "cases": cases,
        "metrics": None,
        "reason": reason,
        "failures": failures,
        "observations": scenario.observed,
        "requests": scenario.requests,
        "elapsed_seconds": clock() - started,
    }
    validate_safe(result)
    return result


def run_e2e_trials(
    *,
    case: E2ECase,
    client: httpx.Client,
    state: StateReader,
    inspection: Inspection,
    author_token: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    recorder: JsonlTrialRecorder,
) -> E2ERun:
    trials: list[E2ETrial] = []
    for trial in (1, 2, 3):
        result = run_e2e_trial(
            case=case,
            character_id=e2e_character_id(trial=trial),
            client=client,
            state=state,
            inspection=inspection,
            author_token=author_token,
            clock=clock,
            sleep=sleep,
        )
        row: E2ETrial = {**result, "trial": trial}
        recorder.record(row)
        trials.append(row)
    summary = summarize_trials(cast(list[TrialResult], trials), required_case_ids=set(STEPS))
    return {"verdict": cast(str, summary["verdict"]), "trials": trials}
