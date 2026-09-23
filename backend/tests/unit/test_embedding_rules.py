from datetime import timedelta

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine.default import DefaultDialect

from app.core.config import Environment, Settings
from app.db.vector import Vector, from_pgvector_text, to_pgvector_text
from app.domain.embeddings import document_hash, render_fact_document, retry_delay

DIALECT = DefaultDialect()


def test_fact_document_is_readable_and_deterministic() -> None:
    prop = "billing.credit_limit"
    first = render_fact_document("customer", "customer-991", prop, {"b": 1, "a": 2})
    second = render_fact_document("customer", "customer-991", prop, {"a": 2, "b": 1})

    assert first == second
    assert first == 'customer customer-991\nbilling / credit limit: {"a": 2, "b": 1}'


def test_fact_document_keeps_unicode() -> None:
    assert "Zürich" in render_fact_document("customer", "c-1", "city", "Zürich")


def test_document_hash_changes_with_the_value() -> None:
    a = render_fact_document("customer", "c-1", "credit_limit", 2000)
    b = render_fact_document("customer", "c-1", "credit_limit", 5000)

    assert document_hash(a) != document_hash(b)
    assert len(document_hash(a)) == 64


@pytest.mark.parametrize(("attempts", "expected"), [(0, 0), (1, 10), (2, 20), (3, 40), (10, 900)])
def test_retry_delay_is_exponential_and_capped(attempts: int, expected: float) -> None:
    assert retry_delay(attempts, base_seconds=10, cap_seconds=900) == timedelta(seconds=expected)


# --- vector column type ----------------------------------------------------------


def test_vector_text_round_trip() -> None:
    values = [0.25, -1.5, 3.0]

    assert from_pgvector_text(to_pgvector_text(values)) == values
    assert from_pgvector_text("[]") == []


def test_vector_bind_checks_dimensions() -> None:
    process = Vector(3).bind_processor(DIALECT)

    assert process([1, 2, 3]) == "[1.0,2.0,3.0]"
    assert process(None) is None
    with pytest.raises(ValueError, match="dimensions"):
        process([1, 2])


def test_vector_result_parses_text() -> None:
    process = Vector(2).result_processor(DIALECT, None)

    assert process("[0.5,1]") == [0.5, 1.0]
    assert process(None) is None


def test_vector_column_spec() -> None:
    assert Vector(1536).get_col_spec() == "vector(1536)"


def test_malformed_vector_text_is_rejected() -> None:
    with pytest.raises(ValueError, match="pgvector"):
        from_pgvector_text("1,2,3")


# --- settings -------------------------------------------------------------------


def test_openai_provider_requires_a_key() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, embedding_provider="openai")


def test_deployed_environments_require_real_embeddings() -> None:
    with pytest.raises(ValidationError, match="openai embedding provider"):
        Settings(
            _env_file=None,
            environment=Environment.STAGING,
            db_password=SecretStr("x"),
        )


def test_openai_base_url_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXTLEDGER_OPENAI_BASE_URL", "not a url")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
