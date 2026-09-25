"""Prometheus metrics endpoint, HTTP metrics labels, log/trace correlation."""

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr, ValidationError

from app.core.config import Environment, Settings
from app.core.logging import CorrelationIdFilter
from app.main import create_app


async def test_metrics_expose_route_templates_not_raw_paths(client: AsyncClient) -> None:
    await client.get("/api/v1/health")
    org = "11111111-1111-4111-8111-111111111111"
    await client.get(f"/api/v1/organizations/{org}/contradictions")

    body = (await client.get("/metrics")).text

    assert (
        'contextledger_http_requests_total{method="GET",route="/api/v1/health",status="200"}'
        in body
    )
    assert 'route="/api/v1/organizations/{organization_id}/contradictions"' in body
    assert org not in body  # no tenant identifiers in metrics
    assert "contextledger_http_request_duration_seconds_bucket" in body


async def test_unmatched_paths_share_one_label(client: AsyncClient) -> None:
    await client.get("/api/v1/nope-1")
    await client.get("/api/v1/nope-2")

    body = (await client.get("/metrics")).text

    assert 'route="unmatched"' in body
    assert "nope-1" not in body


async def test_a_metrics_token_is_enforced(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"metrics_token": SecretStr("s3cret")}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/metrics")).status_code == 401
        assert (
            await c.get("/metrics", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        ).status_code == 200
    await app.state.db_engine.dispose()


async def test_metrics_can_be_disabled(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"metrics_enabled": False}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/metrics")).status_code == 404
    await app.state.db_engine.dispose()


def test_deployed_environments_need_a_metrics_token() -> None:
    with pytest.raises(ValidationError, match="METRICS_TOKEN"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            db_password=SecretStr("x"),
            embedding_provider="openai",
            openai_api_key=SecretStr("sk-test-not-real"),
            neo4j_password=SecretStr("y"),
            auth_mode="jwt",
            jwt_signing_key=SecretStr("-----BEGIN PRIVATE KEY-----test"),
            redis_url=SecretStr("rediss://cache:6379/0"),
        )


def test_logs_carry_the_active_trace_and_span_ids() -> None:
    tracer = TracerProvider().get_tracer("test")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    with tracer.start_as_current_span("work") as span:
        CorrelationIdFilter().filter(record)
        context = span.get_span_context()
    assert getattr(record, "trace_id", None) == f"{context.trace_id:032x}"
    assert getattr(record, "span_id", None) == f"{context.span_id:016x}"


def test_logs_without_a_span_have_no_trace_ids() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    CorrelationIdFilter().filter(record)
    assert getattr(record, "trace_id", "x") is None and getattr(record, "span_id", "x") is None
