"""Error contract (RFC 9457) and authentication, without a database."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Environment, Settings
from app.domain.errors import (
    ConflictError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.main import create_app

PROBLEM = "application/problem+json"
USER_HEADER = "X-ContextLedger-User-Id"


def raising_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/test/raise/{kind}")
    async def raise_error(kind: str) -> None:
        errors = {
            "not_found": NotFoundError("thing not found"),
            "conflict": ConflictError("already exists"),
            "invariant": InvariantViolationError("would leave no admin"),
            "denied": PermissionDeniedError("not a member of this organization"),
            "invalid": ValidationFailedError("slug is invalid"),
        }
        if kind in errors:
            raise errors[kind]
        raise RuntimeError("secret internals: db password is hunter2")

    return app


@pytest.fixture
async def raising_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = raising_app(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await app.state.db_engine.dispose()


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("not_found", 404, "not_found"),
        ("conflict", 409, "conflict"),
        ("invariant", 409, "invariant_violation"),
        ("denied", 403, "permission_denied"),
        ("invalid", 422, "validation_failed"),
    ],
)
async def test_domain_errors_become_problem_details(
    raising_client: AsyncClient, kind: str, status: int, code: str
) -> None:
    response = await raising_client.get(f"/test/raise/{kind}", headers={"X-Correlation-ID": "c-1"})

    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["instance"] == f"/test/raise/{kind}"
    assert body["correlation_id"] == "c-1"
    assert body["title"]


async def test_unexpected_errors_are_generic_500s(raising_client: AsyncClient) -> None:
    response = await raising_client.get("/test/raise/boom")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["correlation_id"] == response.headers["X-Correlation-ID"]
    assert "hunter2" not in response.text


async def test_unknown_routes_are_problem_details(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nope")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "not_found"


async def test_request_validation_lists_the_fields(client: AsyncClient) -> None:
    response = await client.post("/api/v1/users", json={"email": "a@example.com", "extra": 1})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "request_invalid"
    assert {e["field"] for e in body["errors"]} == {"display_name", "extra"}


@pytest.mark.parametrize("header", [None, "not-a-uuid"])
async def test_missing_or_bad_identity_is_401(client: AsyncClient, header: str | None) -> None:
    headers = {} if header is None else {USER_HEADER: header}

    response = await client.get("/api/v1/users/me", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "authentication_required"


async def test_jwt_mode_refuses_development_headers() -> None:
    app = create_app(Settings(_env_file=None, environment=Environment.TEST, auth_mode="jwt"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/users/me", headers={USER_HEADER: str(uuid4())})
    await app.state.db_engine.dispose()

    assert response.status_code == 401


async def test_openapi_documents_every_area_and_the_problem_schema(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()

    tags = {tag for path in spec["paths"].values() for op in path.values() for tag in op["tags"]}
    assert tags == {
        "health",
        "auth",
        "users",
        "organizations",
        "sources",
        "facts",
        "evidence",
        "search",
        "decisions",
        "provenance",
    }
    assert "Problem" in spec["components"]["schemas"]
    receipt = spec["paths"][
        "/api/v1/organizations/{organization_id}/decisions/{decision_id}/receipt"
    ]
    assert set(receipt["get"]["responses"]) >= {"200", "401", "403", "404"}
