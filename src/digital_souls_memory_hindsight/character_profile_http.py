"""Character profile endpoints and their shared exception boundary."""

from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .character_profile.domain import CharacterProfile, ProfileValidationError
from .character_profile.ports import ProfileNotFound
from .character_profile.service import CharacterProfileService
from .evidence.domain import EvidenceValidationError
from .evidence.ports import EvidenceNotFound, EvidenceUnavailable
from .voice.domain import Span

Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]+$")]
PathIdentifier = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


class EntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: Identifier
    start: Annotated[StrictInt, Field(ge=0)]
    end: Annotated[StrictInt, Field(gt=0)]

    def to_span(self) -> Span:
        return Span(self.turn_id, self.start, self.end)


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    char_limit: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
    entries: list[EntryRequest]


class ProfileResponse(BaseModel):
    character_id: str
    char_limit: int
    revision: int
    entries: list[EntryRequest]
    updated_at: AwareDatetime

    @classmethod
    def from_value(cls, value: CharacterProfile) -> "ProfileResponse":
        return cls(**asdict(value))


class ProfileRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await endpoint(request)
            except (RequestValidationError, ProfileValidationError):
                return JSONResponse({"detail": "Invalid profile value"}, status_code=422)
            except (ProfileNotFound, EvidenceNotFound):
                return JSONResponse({"detail": "Profile not found"}, status_code=404)
            except (EvidenceUnavailable, EvidenceValidationError):
                return JSONResponse({"detail": "Profile unavailable"}, status_code=503)

        return handle


def character_profile_router(service: Callable[[], CharacterProfileService]) -> APIRouter:
    router = APIRouter(route_class=ProfileRoute)

    @router.put("/characters/{character_id}/profile")
    def put(character_id: PathIdentifier, body: ProfileRequest) -> ProfileResponse:
        value = service().put(
            character_id, body.char_limit, tuple(entry.to_span() for entry in body.entries)
        )
        return ProfileResponse.from_value(value)

    @router.get("/characters/{character_id}/profile")
    def get(character_id: PathIdentifier) -> ProfileResponse:
        return ProfileResponse.from_value(service().get(character_id))

    return router
