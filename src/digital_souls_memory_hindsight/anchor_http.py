"""Character-scoped anchor API and its protocol error boundary."""

from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr

from .anchors.ports import AnchorConflict, AnchorNotFound
from .anchors.service import AnchorProjection, AnchorService
from .evidence.domain import EvidenceValidationError
from .evidence.ports import EvidenceNotFound, EvidenceUnavailable

IDENTIFIER_PATTERN = r"^[A-Za-z0-9_-]+$"
Identifier = Annotated[StrictStr, Field(pattern=IDENTIFIER_PATTERN)]
PathIdentifier = Annotated[str, Path(pattern=IDENTIFIER_PATTERN)]
Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]


class AnchorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_id: Identifier
    turn_id: Identifier
    kind: Literal["identity_statement", "commitment", "nickname", "anniversary"]
    reason: Nonblank
    approved_by: Nonblank


class AnchorResponse(BaseModel):
    character_id: str
    anchor_id: str
    turn_id: str
    kind: str
    reason: str
    approved_by: str
    registered_at: AwareDatetime
    text: str

    @classmethod
    def from_projection(cls, value: AnchorProjection) -> "AnchorResponse":
        anchor, text = value
        return cls(**asdict(anchor), text=text)


class AnchorRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await endpoint(request)
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid voice value"}, status_code=422)
            except AnchorConflict:
                return JSONResponse({"detail": "Voice already exists"}, status_code=409)
            except (AnchorNotFound, EvidenceNotFound):
                return JSONResponse({"detail": "Voice not found"}, status_code=404)
            except (EvidenceUnavailable, EvidenceValidationError):
                return JSONResponse({"detail": "Voice unavailable"}, status_code=503)

        return handle


def anchor_router(service: Callable[[], AnchorService]) -> APIRouter:
    router = APIRouter(route_class=AnchorRoute)

    @router.post("/characters/{character_id}/verbatim-anchors", status_code=201)
    def register(character_id: PathIdentifier, body: AnchorRequest) -> AnchorResponse:
        return AnchorResponse.from_projection(service().register(character_id, **body.model_dump()))

    @router.get("/characters/{character_id}/verbatim-anchors")
    def list_anchors(character_id: PathIdentifier) -> list[AnchorResponse]:
        return [AnchorResponse.from_projection(value) for value in service().list(character_id)]

    @router.get("/characters/{character_id}/verbatim-anchors/{anchor_id}")
    def get(character_id: PathIdentifier, anchor_id: PathIdentifier) -> AnchorResponse:
        return AnchorResponse.from_projection(service().get(character_id, anchor_id))

    return router
