from datetime import datetime

from httpx import ASGITransport, AsyncClient

from app.core.config import Environment, Settings
from app.main import create_app


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "ContextLedger"
    assert body["environment"] == "test"
    assert isinstance(body["version"], str)
    assert datetime.fromisoformat(body["timestamp"]).tzinfo is not None


async def test_health_body_has_exactly_the_documented_fields(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert set(response.json()) == {"status", "service", "version", "environment", "timestamp"}


async def test_health_sets_a_correlation_id_header(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Correlation-ID": "demo-123"})

    assert response.headers["X-Correlation-ID"] == "demo-123"


async def test_unknown_routes_return_404_with_a_correlation_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.headers["X-Correlation-ID"]


async def test_health_is_only_served_under_the_versioned_prefix(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 404


async def test_openapi_documents_the_health_endpoint(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]


async def test_docs_can_be_disabled() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST, docs_enabled=False)
    transport = ASGITransport(app=create_app(settings))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/api/v1/health")).status_code == 200
