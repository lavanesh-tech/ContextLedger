"""Security headers, request body limit, database TLS settings (Phase 28)."""

import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.config import Environment, Settings
from app.core.security import SecurityHeadersMiddleware
from app.db.session import ssl_context


async def test_api_responses_carry_security_headers(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers  # local: no TLS in front


async def test_error_responses_carry_security_headers_too(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_interactive_docs_are_not_blocked_by_the_api_csp(client: AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"


async def test_hsts_is_sent_only_when_enabled() -> None:
    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    for hsts in (True, False):
        app = SecurityHeadersMiddleware(Starlette(routes=[Route("/", ok)]), hsts=hsts)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            response = await c.get("/")
        assert ("strict-transport-security" in response.headers) is hsts


async def test_oversized_body_is_rejected_with_413_before_parsing(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/users",
        content=b"{" + b" " * (1_048_576 + 10) + b"}",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


async def test_streamed_oversized_body_is_rejected_too(client: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b" " * 600_000

    response = await client.post(
        "/api/v1/users", content=chunks(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


async def test_normal_bodies_pass_through(client: AsyncClient) -> None:
    response = await client.post("/api/v1/users", json={"email": "not-an-email"})

    assert response.status_code == 422  # reached validation, not the size limit


def test_database_tls_is_mandatory_outside_local() -> None:
    with pytest.raises(ValidationError, match="DB_SSL_MODE"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            process_role="batch",
            db_password=SecretStr("x"),
            embedding_provider="openai",
            openai_api_key=SecretStr("sk-test-not-real"),
            auth_mode="jwt",
        )


def test_verify_full_requires_a_ca_bundle() -> None:
    with pytest.raises(ValidationError, match="DB_SSL_ROOT_CERT"):
        Settings(_env_file=None, db_ssl_mode="verify-full")


def test_ssl_context_per_mode(tmp_path: Path) -> None:
    assert ssl_context(Settings(_env_file=None)) is False

    required = ssl_context(Settings(_env_file=None, db_ssl_mode="require"))
    assert isinstance(required, ssl.SSLContext)
    assert required.verify_mode == ssl.CERT_NONE

    ca = Path(ssl.get_default_verify_paths().cafile or "")
    if not ca.is_file():
        pytest.skip("no system CA bundle to load")
    verified = ssl_context(Settings(_env_file=None, db_ssl_mode="verify-full", db_ssl_root_cert=ca))
    assert isinstance(verified, ssl.SSLContext)
    assert verified.verify_mode == ssl.CERT_REQUIRED
    assert verified.check_hostname is True
