"""Voice exemplar HTTP endpoints and their shared exception boundary."""

from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .evidence.domain import EvidenceValidationError
from .evidence.ports import EvidenceNotFound, EvidenceUnavailable
from .voice.domain import Span, VoiceExemplar, VoiceValidationError
from .voice.ports import VoiceConflict, VoiceNotFound
from .voice.service import VoiceService

Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]+$")]
PathIdentifier = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]
Text = Annotated[StrictStr, Field(min_length=1)]
Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]


class SpanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: Identifier
    start: Annotated[StrictInt, Field(ge=0)]
    end: Annotated[StrictInt, Field(gt=0)]

    def to_span(self) -> Span:
        return Span(self.turn_id, self.start, self.end)


class VoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exemplar_id: Identifier
    source: Literal["canonical_seed", "turn"]
    answer: Text
    approved_by: Nonblank
    question: Text | None = None
    answer_span: SpanRequest | None = None
    question_span: SpanRequest | None = None


class VoiceResponse(BaseModel):
    character_id: str
    exemplar_id: str
    source: str
    answer: str
    question: str | None
    answer_span: SpanRequest | None
    question_span: SpanRequest | None
    approved_by: str
    registered_at: AwareDatetime

    @classmethod
    def from_value(cls, value: VoiceExemplar) -> "VoiceResponse":
        return cls(**asdict(value))


class VoiceRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await endpoint(request)
            except (RequestValidationError, VoiceValidationError):
                return JSONResponse({"detail": "Invalid voice value"}, status_code=422)
            except VoiceConflict:
                return JSONResponse({"detail": "Voice already exists"}, status_code=409)
            except (VoiceNotFound, EvidenceNotFound):
                return JSONResponse({"detail": "Voice not found"}, status_code=404)
            except (EvidenceUnavailable, EvidenceValidationError):
                return JSONResponse({"detail": "Voice unavailable"}, status_code=503)

        return handle


def voice_router(service: Callable[[], VoiceService]) -> APIRouter:
    router = APIRouter(route_class=VoiceRoute)

    @router.post("/characters/{character_id}/voice-exemplars", status_code=201)
    def register(character_id: PathIdentifier, body: VoiceRequest) -> VoiceResponse:
        value = service().register(
            character_id,
            body.exemplar_id,
            body.source,
            body.answer,
            body.approved_by,
            question=body.question,
            answer_span=body.answer_span.to_span() if body.answer_span is not None else None,
            question_span=body.question_span.to_span() if body.question_span is not None else None,
        )
        return VoiceResponse.from_value(value)

    @router.get("/characters/{character_id}/voice-exemplars")
    def list_exemplars(
        character_id: PathIdentifier,
        query: Annotated[str | None, Query(min_length=1)] = None,
    ) -> list[VoiceResponse]:
        return [VoiceResponse.from_value(value) for value in service().list(character_id, query)]

    @router.get("/characters/{character_id}/voice-exemplars/{exemplar_id}")
    def get(character_id: PathIdentifier, exemplar_id: PathIdentifier) -> VoiceResponse:
        return VoiceResponse.from_value(service().get(character_id, exemplar_id))

    return router
