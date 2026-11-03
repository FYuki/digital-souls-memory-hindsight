"""Independent CharacterState query and its HTTP exception boundary."""

from collections.abc import Callable, Coroutine
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from .character_state.service import CharacterStateService
from .evidence.domain import EvidenceValidationError
from .evidence.ports import EvidenceNotFound, EvidenceUnavailable

PathIdentifier = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


class StateBody(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    mood: str
    relationship_temperature: str
    current_topic: str


class StateResponse(BaseModel):
    state: StateBody | None


class StateRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await endpoint(request)
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid state query"}, status_code=422)
            except EvidenceNotFound:
                return JSONResponse({"detail": "State scope not found"}, status_code=404)
            except (EvidenceUnavailable, EvidenceValidationError):
                return JSONResponse({"detail": "State unavailable"}, status_code=503)

        return handle


def character_state_router(service: Callable[[], CharacterStateService]) -> APIRouter:
    router = APIRouter(route_class=StateRoute)

    @router.get("/characters/{character_id}/sessions/{session_id}/state")
    def get(
        character_id: PathIdentifier,
        session_id: PathIdentifier,
        surface: Annotated[str, Query(pattern=r"\S")],
    ) -> StateResponse:
        value = service().get(character_id, session_id, surface)
        return StateResponse(state=None if value is None else StateBody.model_validate(value))

    return router
