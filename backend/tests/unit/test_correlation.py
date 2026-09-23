"""Correlation middleware tests against a bare Starlette app (no FastAPI routing)."""

import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.correlation import (
    CorrelationIdMiddleware,
    get_correlation_id,
    resolve_correlation_id,
)

HEADER = "X-Correlation-ID"


async def _echo(_: Request) -> JSONResponse:
    return JSONResponse({"seen": get_correlation_id()})


async def _boom(_: Request) -> JSONResponse:
    raise RuntimeError("handler failed")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = Starlette(routes=[Route("/echo", _echo), Route("/boom", _boom)])
    app.add_middleware(CorrelationIdMiddleware, header_name=HEADER)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


async def test_generates_an_id_when_the_client_sends_none(client: AsyncClient) -> None:
    response = await client.get("/echo")

    generated = response.headers[HEADER]
    assert uuid.UUID(generated)
    assert response.json() == {"seen": generated}


async def test_reuses_a_valid_client_supplied_id(client: AsyncClient) -> None:
    response = await client.get("/echo", headers={HEADER: "upstream-trace.01"})

    assert response.headers[HEADER] == "upstream-trace.01"
    assert response.json() == {"seen": "upstream-trace.01"}


@pytest.mark.parametrize(
    "unsafe",
    ["", "x" * 129, "abc def", "abc\tdef", '{"json":1}', "id;drop"],
)
def test_unsafe_ids_are_replaced(unsafe: str) -> None:
    resolved = resolve_correlation_id(unsafe)

    assert resolved != unsafe
    assert uuid.UUID(resolved)


async def test_context_is_cleared_after_the_request(client: AsyncClient) -> None:
    await client.get("/echo")

    assert get_correlation_id() is None


async def test_each_request_gets_its_own_id(client: AsyncClient) -> None:
    first = await client.get("/echo")
    second = await client.get("/echo")

    assert first.headers[HEADER] != second.headers[HEADER]


async def test_logs_one_completion_record_per_request(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="contextledger.http")

    await client.get("/echo?secret=do-not-log", headers={HEADER: "req-1"})

    [record] = [r for r in caplog.records if r.name == "contextledger.http"]
    assert record.getMessage() == "request.completed"
    assert record.__dict__["http_method"] == "GET"
    assert record.__dict__["http_path"] == "/echo"
    assert record.__dict__["http_status"] == 200
    assert record.__dict__["duration_ms"] >= 0
    assert "do-not-log" not in str(record.__dict__)


async def test_unhandled_errors_are_logged_as_500(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="contextledger.http")

    response = await client.get("/boom", headers={HEADER: "trace-500"})

    assert response.status_code == 500
    assert response.headers[HEADER] == "trace-500"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["correlation_id"] == "trace-500"
    assert "handler failed" not in response.text  # no internals leak
    [record] = [r for r in caplog.records if r.name == "contextledger.http"]
    assert record.levelno == logging.ERROR
    assert record.__dict__["http_status"] == 500
    assert record.exc_info is not None
