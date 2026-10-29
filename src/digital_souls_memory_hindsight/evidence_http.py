"""Core-facing evidence API; protocol errors are translated at this boundary."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .evidence.domain import DeliveryReceipt, EvidenceValidationError, Session, TurnInput
from .evidence.ports import EvidenceConflict, EvidenceNotFound, EvidenceUnavailable
from .evidence.service import EvidenceService

Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]+$")]
Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]


class EvidenceRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await endpoint(request)
            except RequestValidationError as error:
                details = [
                    {key: item[key] for key in ("loc", "type", "msg")} for item in error.errors()
                ]
                return JSONResponse({"detail": details}, status_code=422)
            except (
                EvidenceValidationError,
                EvidenceConflict,
                EvidenceNotFound,
                EvidenceUnavailable,
            ) as error:
                status, detail = {
                    EvidenceValidationError: (422, "Invalid evidence value"),
                    EvidenceConflict: (409, "Evidence already exists"),
                    EvidenceNotFound: (404, "Evidence not found"),
                    EvidenceUnavailable: (503, "Evidence unavailable"),
                }[type(error)]
                return JSONResponse({"detail": detail}, status_code=status)

        return handle


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: Identifier
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    surface: Nonblank
    reports_delivery: StrictBool

    def to_domain(self) -> Session:
        return Session(**self.model_dump())


class SessionResponse(SessionRequest):
    model_config = ConfigDict(from_attributes=True)


class TurnFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: Identifier
    session_id: Identifier
    surface: Nonblank
    actor: Nonblank
    kind: Literal["utterance", "observation", "thought"]
    text: StrictStr
    perceived_by: list[Identifier]
    addressees: list[Identifier]
    reply_to_turn_id: Identifier | None = None
    language: Nonblank
    speech_register: dict[StrictStr, StrictStr] = Field(alias="register")
    sensitivity: Nonblank
    classifier_version: Nonblank
    occurred_at: AwareDatetime


class TurnRequest(TurnFields):
    address_mode: Literal["direct", "broadcast", "self"] | None = None

    def to_domain(self) -> TurnInput:
        values = self.model_dump(by_alias=True)
        values["perceived_by"] = tuple(self.perceived_by)
        values["addressees"] = tuple(self.addressees)
        return TurnInput(**values)


class TurnResponse(TurnFields):
    model_config = ConfigDict(from_attributes=True)
    address_mode: Literal["direct", "broadcast", "self"]
    recorded_at: AwareDatetime


class ReceiptRequest(BaseModel):
    status: Literal["full", "partial", "none"]
    spoken_until: Annotated[StrictInt, Field(ge=0)]
    reported_by: Nonblank
    reported_at: AwareDatetime

    def to_domain(self, turn_id: str) -> DeliveryReceipt:
        return DeliveryReceipt(turn_id=turn_id, **self.model_dump())


class ReceiptResponse(ReceiptRequest):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str


class EndingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ended_at: AwareDatetime
    reported_by: Nonblank


class EndingResponse(EndingRequest):
    model_config = ConfigDict(from_attributes=True)
    session_id: str
    reported_at: AwareDatetime


def evidence_router(service: Callable[[], EvidenceService]) -> APIRouter:
    router = APIRouter(route_class=EvidenceRoute)

    @router.post("/sessions", status_code=201)
    def add_session(body: SessionRequest) -> SessionResponse:
        return SessionResponse.model_validate(service().add_session(body.to_domain()))

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str) -> SessionResponse:
        return SessionResponse.model_validate(service().get_session(session_id))

    @router.post("/sessions/{session_id}/endings", status_code=201)
    def add_ending(session_id: str, body: EndingRequest, response: Response) -> EndingResponse:
        ending, created = service().add_ending(session_id, body.ended_at, body.reported_by)
        response.status_code = 201 if created else 200
        return EndingResponse.model_validate(ending)

    @router.get("/sessions/{session_id}/endings")
    def list_endings(session_id: str) -> list[EndingResponse]:
        return [
            EndingResponse.model_validate(value) for value in service().list_endings(session_id)
        ]

    @router.post("/turns", status_code=201)
    def add_turn(body: TurnRequest) -> TurnResponse:
        return TurnResponse.model_validate(service().add_turn(body.to_domain()))

    @router.get("/turns/{turn_id}")
    def get_turn(turn_id: str) -> TurnResponse:
        return TurnResponse.model_validate(service().get_turn(turn_id))

    @router.post("/turns/{turn_id}/receipts", status_code=201)
    def add_receipt(turn_id: str, body: ReceiptRequest) -> ReceiptResponse:
        return ReceiptResponse.model_validate(service().add_receipt(body.to_domain(turn_id)))

    @router.get("/turns/{turn_id}/receipts")
    def list_receipts(turn_id: str) -> list[ReceiptResponse]:
        return [ReceiptResponse.model_validate(value) for value in service().list_receipts(turn_id)]

    @router.get("/characters/{character_id}/turns")
    def list_turns(character_id: str) -> list[TurnResponse]:
        return [
            TurnResponse.model_validate(value)
            for value in service().list_perceived_turns(character_id)
        ]

    return router
