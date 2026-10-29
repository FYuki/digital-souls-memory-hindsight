"""Core-facing recall API and its protocol error boundary."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from .recall.ports import RecallSourceNotFound, RecallUnavailable
from .recall.service import RecallService

Nonempty = Annotated[StrictStr, Field(min_length=1)]


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: Nonempty
    format: Literal["materials", "assembled"]
    query: Nonempty | None = None
    turn_id: Nonempty | None = None
    sensitivity: Nonempty | None = None
    classifier_version: Nonempty | None = None

    @field_validator("format", mode="before")
    @classmethod
    def strip_format(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if (self.query is None) == (self.turn_id is None):
            raise ValueError("Exactly one query source is required")
        if self.query is not None and (self.sensitivity is None or self.classifier_version is None):
            raise ValueError("Query classification is required")
        return self


class MemoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    kind: str
    content: str
    source_turn_ids: list[str]


class SourceSpan(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    start: int
    end: int
    text: str
    actor: str
    language: str
    truncated: bool
    original_start: int
    original_end: int


class RecallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    provision_id: str
    temperament_version: None = None
    personality_version: None = None
    memories: list[MemoryItem]
    source_spans: list[SourceSpan]
    verbatim_anchors: list[object] = Field(default_factory=list)
    context: str | None


class RecallRoute(APIRoute):
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
            except RecallSourceNotFound:
                return JSONResponse({"detail": "Recall source not found"}, status_code=404)
            except RecallUnavailable:
                return JSONResponse({"detail": "Recall unavailable"}, status_code=503)

        return handle


def recall_router(service: Callable[[], RecallService]) -> APIRouter:
    router = APIRouter(route_class=RecallRoute)

    @router.post("/characters/{character_id}/recall")
    async def recall(character_id: str, body: RecallRequest) -> RecallResponse:
        provision = await service().recall(
            character_id, body.session_id, body.format, body.query, body.turn_id
        )
        return RecallResponse.model_validate(provision)

    return router
