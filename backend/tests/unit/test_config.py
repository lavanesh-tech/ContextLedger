import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Environment, Settings


def test_defaults_are_safe_for_local_development() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "ContextLedger"
    assert settings.environment is Environment.LOCAL
    assert settings.log_level == "INFO"
    assert settings.log_json is True
    assert settings.is_production is False


def test_values_are_read_from_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTEXTLEDGER_ENVIRONMENT", "production")
    monkeypatch.setenv("CONTEXTLEDGER_DB_PASSWORD", "from-env")
    monkeypatch.setenv("CONTEXTLEDGER_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("CONTEXTLEDGER_OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("CONTEXTLEDGER_NEO4J_PASSWORD", "neo4j-from-env")
    monkeypatch.setenv("CONTEXTLEDGER_AUTH_MODE", "jwt")
    monkeypatch.setenv("CONTEXTLEDGER_JWT_SIGNING_KEY", "-----BEGIN PRIVATE KEY-----test")
    monkeypatch.setenv("CONTEXTLEDGER_LOG_LEVEL", "warning")
    monkeypatch.setenv("CONTEXTLEDGER_DOCS_ENABLED", "false")

    settings = Settings(_env_file=None)

    assert settings.environment is Environment.PRODUCTION
    assert settings.is_production is True
    assert settings.log_level == "WARNING"  # normalised to upper case
    assert settings.docs_enabled is False


def test_unprefixed_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert Settings(_env_file=None).log_level == "INFO"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "prod"),
        ("log_level", "VERBOSE"),
        ("app_name", "x" * 101),
        ("correlation_id_header", "X-Bad Header"),
        ("db_port", "70000"),
        ("db_pool_size", "0"),
    ],
)
def test_invalid_values_fail_fast(monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    monkeypatch.setenv(f"CONTEXTLEDGER_{field.upper()}", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_are_immutable() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_database_url_is_built_from_parts_and_escapes_the_password() -> None:
    settings = Settings(
        _env_file=None,
        db_host="db.internal",
        db_port=6543,
        db_user="app",
        db_password=SecretStr("p@ss:w/rd"),
        db_name="ledger",
    )

    url = settings.database_url

    assert url.drivername == "postgresql+asyncpg"
    assert (url.host, url.port) == ("db.internal", 6543)
    assert (url.username, url.database) == ("app", "ledger")
    assert url.password == "p@ss:w/rd"
    assert "p%40ss%3Aw%2Frd" in url.render_as_string(hide_password=False)


def test_password_never_appears_in_repr_or_default_url_rendering() -> None:
    settings = Settings(_env_file=None, db_password=SecretStr("top-secret"))

    assert "top-secret" not in repr(settings)
    assert "top-secret" not in str(settings.database_url)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_require_a_database_password(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("CONTEXTLEDGER_ENVIRONMENT", environment)
    monkeypatch.delenv("CONTEXTLEDGER_DB_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="DB_PASSWORD"):
        Settings(_env_file=None)


def test_local_and_test_environments_allow_an_empty_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTEXTLEDGER_DB_PASSWORD", raising=False)

    settings = Settings(_env_file=None, environment=Environment.TEST)

    assert settings.db_password.get_secret_value() == ""


def test_deployed_environments_require_a_neo4j_password() -> None:
    with pytest.raises(ValidationError, match="NEO4J_PASSWORD"):
        Settings(
            _env_file=None,
            environment=Environment.STAGING,
            db_password=SecretStr("x"),
            embedding_provider="openai",
            openai_api_key=SecretStr("sk-test-not-real"),
        )


@pytest.mark.parametrize("uri", ["http://neo4j:7474", "neo4j", "bolt:/x"])
def test_neo4j_uri_scheme_is_validated(uri: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, neo4j_uri=uri)


def test_empty_environment_values_mean_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXTLEDGER_MCP_ORGANIZATION_ID", "")
    monkeypatch.setenv("CONTEXTLEDGER_LOG_LEVEL", "")

    settings = Settings(_env_file=None)

    assert settings.mcp_organization_id is None
    assert settings.log_level == "INFO"


def test_header_authentication_is_refused_in_deployed_environments() -> None:
    with pytest.raises(ValidationError, match="development-headers"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            db_password=SecretStr("x"),
            embedding_provider="openai",
            openai_api_key=SecretStr("sk-test-not-real"),
            neo4j_password=SecretStr("y"),
        )
