"""RFC 9457 problem details for every error response.

Every error body looks like::

    {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found",
     "detail": "decision not found", "instance": "/api/v1/...", "correlation_id": "..."}

``code`` is stable and machine-readable; ``detail`` is for humans. Validation
errors add ``errors`` (field, message). Unexpected errors are answered by the
correlation middleware with a generic 500 (no internals).
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.ai.agent.investigator import InvestigationError
from app.core.correlation import get_correlation_id
from app.domain.errors import (
    ConflictError,
    DomainError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.services.answers import AnswerGenerationError

PROBLEM_JSON = "application/problem+json"

STATUS_BY_ERROR: dict[type[DomainError], int] = {
    ValidationFailedError: 422,
    NotFoundError: 404,
    ConflictError: 409,
    InvariantViolationError: 409,
    PermissionDeniedError: 403,
}


class FieldError(BaseModel):
    field: str
    message: str


class Problem(BaseModel):
    """Error response body (RFC 9457)."""

    model_config = ConfigDict(extra="forbid")

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str | None = None
    instance: str | None = None
    correlation_id: str | None = None
    errors: list[FieldError] | None = None


class AuthenticationRequiredError(Exception):
    """No (valid) credentials were presented. Maps to 401."""


class RateLimitedError(Exception):
    """The caller exceeded its request budget. Maps to 429 with Retry-After."""

    def __init__(self, *, limit: int, retry_after_seconds: int) -> None:
        super().__init__(f"rate limit of {limit} requests per minute exceeded")
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


class ServiceUnavailableError(Exception):
    """An optional dependency (e.g. Neo4j) is not configured or reachable. Maps to 503."""


def problem_response(
    request: Request,
    status: int,
    code: str,
    detail: str | None,
    *,
    errors: list[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    problem = Problem(
        title=HTTPStatus(status).phrase,
        status=status,
        code=code,
        detail=detail,
        instance=request.url.path,
        correlation_id=get_correlation_id(),
        errors=errors,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


def status_for(error: DomainError) -> int:
    for cls in type(error).__mro__:
        if cls in STATUS_BY_ERROR:
            return STATUS_BY_ERROR[cls]
    return 400


async def _domain(request: Request, exc: Exception) -> JSONResponse:
    error = cast(DomainError, exc)  # registered for DomainError only
    return problem_response(request, status_for(error), error.code, str(error))


async def _validation(request: Request, exc: Exception) -> JSONResponse:
    validation = cast(RequestValidationError, exc)
    errors = [
        FieldError(
            field=".".join(str(part) for part in error["loc"] if part != "body"),
            message=str(error["msg"]),
        )
        for error in validation.errors()
    ]
    return problem_response(
        request, 422, "request_invalid", "request validation failed", errors=errors
    )


async def _http(request: Request, exc: Exception) -> JSONResponse:
    http = cast(StarletteHTTPException, exc)
    code = HTTPStatus(http.status_code).phrase.lower().replace(" ", "_")
    detail = http.detail if isinstance(http.detail, str) else None
    return problem_response(request, http.status_code, code, detail, headers=http.headers)


async def _unauthenticated(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(
        request, 401, "authentication_required", str(exc), headers={"WWW-Authenticate": "Bearer"}
    )


async def _rate_limited(request: Request, exc: Exception) -> JSONResponse:
    limited = cast(RateLimitedError, exc)
    return problem_response(
        request,
        429,
        "rate_limited",
        str(limited),
        headers={
            "Retry-After": str(limited.retry_after_seconds),
            "RateLimit-Limit": str(limited.limit),
            "RateLimit-Remaining": "0",
        },
    )


async def _generation_failed(request: Request, exc: Exception) -> JSONResponse:
    failure = cast(AnswerGenerationError | InvestigationError, exc)
    # Never an invented answer: the client learns that generation failed, and why
    # in general terms (the provider error class), with nothing sensitive.
    return problem_response(
        request,
        503,
        "generation_unavailable",
        f"the language model could not produce an answer ({failure.cause})",
        headers={"Retry-After": "5"},
    )


async def _unavailable(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(request, 503, "service_unavailable", str(exc))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain)
    app.add_exception_handler(RequestValidationError, _validation)
    app.add_exception_handler(StarletteHTTPException, _http)
    app.add_exception_handler(AuthenticationRequiredError, _unauthenticated)
    app.add_exception_handler(RateLimitedError, _rate_limited)
    app.add_exception_handler(ServiceUnavailableError, _unavailable)
    app.add_exception_handler(AnswerGenerationError, _generation_failed)
    app.add_exception_handler(InvestigationError, _generation_failed)


# Documented on every endpoint that can fail with these statuses.
def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {
        status: {"model": Problem, "description": HTTPStatus(status).phrase} for status in statuses
    }
