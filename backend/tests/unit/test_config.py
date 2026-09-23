import pytest
from pydantic import ValidationError

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
        ("app_name", ""),
        ("correlation_id_header", "X-Bad Header"),
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
