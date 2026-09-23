"""Readiness behaviour when PostgreSQL is unreachable (no database needed).

The database-backed "ready" path is covered in tests/integration.
"""

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Environment, Settings
from app.main import create_app

SECRET = "never-leak-this-password"


def _unreachable_db_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        db_host="127.0.0.1",
        db_port=1,  # nothing listens here: connection refused immediately
        db_password=SecretStr(SECRET),
        db_connect_timeout_seconds=1.0,
        readiness_timeout_seconds=2.0,
    )


async def test_readiness_is_503_when_the_database_is_unreachable() -> None:
    app = create_app(_unreachable_db_settings())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/api/v1/health/ready")
    finally:
        await app.state.db_engine.dispose()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["database"]["status"] == "down"
    assert isinstance(body["database"]["error"], str)
    assert body["migrations"]["current_revision"] is None
    assert body["migrations"]["up_to_date"] is False
    assert SECRET not in response.text


async def test_liveness_stays_up_while_the_database_is_down() -> None:
    app = create_app(_unreachable_db_settings())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/api/v1/health")
    finally:
        await app.state.db_engine.dispose()

    assert response.status_code == 200


async def test_readiness_reports_the_expected_schema_revision(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/health/ready")).json()

    # Comes from the migration files, whether or not a database is running.
    assert body["migrations"]["expected_revision"] is not None


async def test_openapi_documents_the_503_readiness_response(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()

    assert "503" in spec["paths"]["/api/v1/health/ready"]["get"]["responses"]
