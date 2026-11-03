"""Identity HTTP schemas and their scoped protocol error boundary."""

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictStr

from .identity.ports import (
    IdentityConflict,
    IdentityInconsistent,
    IdentityNotFound,
    IdentityUnavailable,
)
from .identity.service import IdentityService

PathIdentifier = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]
Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]
Aspect = Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)]


class AspectsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    Volatility: Aspect
    Withdrawal: Aspect
    Compassion: Aspect
    Politeness: Aspect
    Industriousness: Aspect
    Orderliness: Aspect
    Enthusiasm: Aspect
    Assertiveness: Aspect
    Intellect: Aspect
    Openness: Aspect


class CoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: list[Nonblank]
    prohibitions: list[Nonblank]


class TemperamentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: Nonblank


class SeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspects: AspectsInput
    renderer_version: Nonblank


class CoreResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    version: int
    values: list[str]
    prohibitions: list[str]
    registered_at: datetime


class TemperamentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    version: int
    description: str
    registered_at: datetime


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    version: int
    description: str
    renderer_version: str
    temperament_version: int


class CurrentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    core_identity: CoreResponse | None
    temperament: TemperamentResponse | None
    personality_profile: ProfileResponse | None


class IdentityRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await endpoint(request)
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid identity value"}, status_code=422)
            except IdentityConflict:
                return JSONResponse({"detail": "Identity already exists"}, status_code=409)
            except IdentityNotFound:
                return JSONResponse({"detail": "Identity not found"}, status_code=404)
            except IdentityInconsistent as error:
                return JSONResponse(
                    {
                        "detail": "Identity inconsistent",
                        "consistency_review": {
                            "consistent": error.review.consistent,
                            "reason": error.review.reason,
                        },
                    },
                    status_code=422,
                )
            except IdentityUnavailable:
                return JSONResponse({"detail": "Identity unavailable"}, status_code=503)

        return handle


def identity_router(service: Callable[[], IdentityService]) -> APIRouter:
    router = APIRouter(route_class=IdentityRoute)

    @router.post("/characters/{character_id}/core-identities", status_code=201)
    def register_core(character_id: PathIdentifier, body: CoreRequest) -> CoreResponse:
        return CoreResponse.model_validate(
            service().register_core(character_id, tuple(body.values), tuple(body.prohibitions))
        )

    @router.post("/characters/{character_id}/temperaments", status_code=201)
    def register_temperament(
        character_id: PathIdentifier, body: TemperamentRequest
    ) -> TemperamentResponse:
        return TemperamentResponse.model_validate(
            service().register_temperament(character_id, body.description)
        )

    @router.post("/characters/{character_id}/personality-profiles/author-seed", status_code=201)
    def seed(character_id: PathIdentifier, body: SeedRequest) -> ProfileResponse:
        return ProfileResponse.model_validate(
            service().author_seed(character_id, body.aspects.model_dump(), body.renderer_version)
        )

    @router.get("/characters/{character_id}/identity/current")
    def current(character_id: PathIdentifier) -> CurrentResponse:
        return CurrentResponse.model_validate(service().current(character_id))

    @router.get("/characters/{character_id}/core-identities/{version}")
    def get_core(character_id: PathIdentifier, version: int) -> CoreResponse:
        return CoreResponse.model_validate(service().get_core(character_id, version))

    @router.get("/characters/{character_id}/temperaments/{version}")
    def get_temperament(character_id: PathIdentifier, version: int) -> TemperamentResponse:
        return TemperamentResponse.model_validate(service().get_temperament(character_id, version))

    @router.get("/characters/{character_id}/personality-profiles/{version}")
    def get_profile(character_id: PathIdentifier, version: int) -> ProfileResponse:
        return ProfileResponse.model_validate(service().get_profile(character_id, version))

    return router
