"""Author-only deletion API, with authorization before body parsing."""

import hmac
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from .deletion.domain import DeletionStatus
from .deletion.ports import DeletionConflict, DeletionNotFound, DeletionTargetNotFound
from .deletion.service import DeletionService
from .evidence.domain import EvidenceValidationError
from .evidence.ports import EvidenceUnavailable

Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]


@dataclass(frozen=True)
class AuthorCredentials:
    author_id: str
    token: str = field(repr=False)


class DeletionRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Nonblank
    target_turn_ids: Annotated[list[Nonblank], Field(min_length=1)] | None = None
    user_id: Nonblank | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if (self.target_turn_ids is None) == (self.user_id is None):
            raise ValueError("Exactly one deletion selection is required")
        return self


class DeletionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    request_id: str
    requested_by: str
    target_turn_ids: list[str]
    status: DeletionStatus
    cascade_result: dict[str, object] | None
    error: str | None
    requested_at: datetime
    completed_at: datetime | None


def deletion_router(
    service: Callable[[], DeletionService], author: AuthorCredentials | None
) -> APIRouter:
    class DeletionRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
            endpoint = super().get_route_handler()

            async def handle(request: Request) -> Response:
                if author is None:
                    return JSONResponse({"detail": "Deletion unavailable"}, status_code=503)
                scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
                if (
                    not separator
                    or scheme.lower() != "bearer"
                    or not token
                    or not hmac.compare_digest(token.encode(), author.token.encode())
                ):
                    return JSONResponse({"detail": "Author required"}, status_code=401)
                try:
                    return await endpoint(request)
                except RequestValidationError as error:
                    details = [
                        {key: item[key] for key in ("loc", "type", "msg")}
                        for item in error.errors()
                    ]
                    return JSONResponse({"detail": details}, status_code=422)
                except DeletionTargetNotFound:
                    return JSONResponse({"detail": "Deletion target not found"}, status_code=422)
                except DeletionConflict:
                    return JSONResponse({"detail": "Deletion conflict"}, status_code=409)
                except DeletionNotFound:
                    return JSONResponse({"detail": "Deletion request not found"}, status_code=404)
                except (EvidenceUnavailable, EvidenceValidationError):
                    return JSONResponse({"detail": "Deletion unavailable"}, status_code=503)

            return handle

    router = APIRouter(route_class=DeletionRoute)

    @router.post("/deletion-requests", status_code=202)
    def accept(body: DeletionRequestBody) -> DeletionResponse:
        assert author is not None
        request = service().accept(
            body.request_id,
            author.author_id,
            tuple(body.target_turn_ids) if body.target_turn_ids is not None else None,
            body.user_id,
        )
        return DeletionResponse.model_validate(request)

    @router.get("/deletion-requests/{request_id}")
    def get(request_id: str) -> DeletionResponse:
        return DeletionResponse.model_validate(service().get(request_id))

    return router
