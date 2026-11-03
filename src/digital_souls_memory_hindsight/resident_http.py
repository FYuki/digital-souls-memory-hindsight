"""Core-facing resident requests, public DTOs and protocol errors."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Self

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from .evidence.domain import EvidenceValidationError
from .evidence.ports import EvidenceNotFound, EvidenceUnavailable
from .recall.ports import ProvisionConflict
from .resident.domain import ResidentContext, ResidentProfile
from .resident.service import ResidentService

Nonempty = Annotated[StrictStr, Field(min_length=1)]


class ResidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: Nonempty
    surface: Nonempty
    query: Nonempty | None = None
    sensitivity: Nonempty | None = None
    classifier_version: Nonempty | None = None

    @model_validator(mode="after")
    def validate_classification(self) -> Self:
        if self.query is not None and (self.sensitivity is None or self.classifier_version is None):
            raise ValueError("Query classification is required")
        return self


class CoreResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    values: list[str]
    prohibitions: list[str]


class DescriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    description: str


class ExemplarResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    exemplar_id: str
    question: str | None
    answer: str


class ProfileEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    start: int
    end: int
    text: str


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    revision: int
    entries: list[ProfileEntryResponse]


class ResidentResponse(BaseModel):
    provision_id: str
    core_identity: CoreResponse | None
    temperament: DescriptionResponse | None
    personality_profile: DescriptionResponse | None
    voice_exemplars: list[ExemplarResponse]
    character_profile: ProfileResponse | None
    motivations: list[object]

    @classmethod
    def from_context(cls, value: ResidentContext) -> Self:
        identity = value.identity
        profile: ResidentProfile | None = value.character_profile
        return cls(
            provision_id=value.provision_id,
            core_identity=None
            if identity.core_identity is None
            else CoreResponse.model_validate(identity.core_identity),
            temperament=None
            if identity.temperament is None
            else DescriptionResponse.model_validate(identity.temperament),
            personality_profile=None
            if identity.personality_profile is None
            else DescriptionResponse.model_validate(identity.personality_profile),
            voice_exemplars=[ExemplarResponse.model_validate(v) for v in value.voice_exemplars],
            character_profile=None if profile is None else ProfileResponse.model_validate(profile),
            motivations=[],
        )


class ResidentRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await endpoint(request)
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid resident request"}, status_code=422)
            except EvidenceNotFound:
                return JSONResponse({"detail": "Resident source not found"}, status_code=404)
            except ProvisionConflict:
                return JSONResponse({"detail": "Provision already exists"}, status_code=409)
            except (EvidenceUnavailable, EvidenceValidationError):
                return JSONResponse({"detail": "Resident unavailable"}, status_code=503)

        return handle


def resident_router(service: Callable[[], ResidentService]) -> APIRouter:
    router = APIRouter(route_class=ResidentRoute)

    @router.post("/characters/{character_id}/resident")
    def provide(character_id: str, body: ResidentRequest) -> ResidentResponse:
        value = service().provide(
            character_id,
            body.session_id,
            body.surface,
            body.query,
            body.sensitivity,
            body.classifier_version,
        )
        return ResidentResponse.from_context(value)

    return router
