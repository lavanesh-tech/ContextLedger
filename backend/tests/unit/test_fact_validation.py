from decimal import Decimal

import pytest

from app.domain.errors import ValidationFailedError
from app.domain.validation import (
    normalize_confidence,
    normalize_external_id,
    normalize_identifier,
    normalize_uri,
    validate_authority,
    validate_fact_value,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("credit_limit", "credit_limit"), (" Billing.Credit_Limit ", "billing.credit_limit")],
)
def test_identifiers_are_normalised(raw: str, expected: str) -> None:
    assert normalize_identifier(raw, field="property") == expected


@pytest.mark.parametrize(
    "raw", ["", "1limit", "credit-limit", "credit limit", "a..b", "a.", "x" * 129]
)
def test_invalid_identifiers_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_identifier(raw, field="property")


def test_external_ids_keep_case_and_are_trimmed() -> None:
    assert normalize_external_id("  Customer-991 ") == "Customer-991"


@pytest.mark.parametrize("raw", ["", "   ", "x" * 257, "bad\nid"])
def test_invalid_external_ids_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_external_id(raw)


@pytest.mark.parametrize(
    "value", [2000, 19.5, "ACTIVE", True, [1, 2], {"amount": 2000, "ccy": "USD"}]
)
def test_json_values_are_accepted(value: object) -> None:
    assert validate_fact_value(value) == value


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), {1, 2}, object()])
def test_non_json_or_null_values_are_rejected(value: object) -> None:
    with pytest.raises(ValidationFailedError):
        validate_fact_value(value)


def test_oversized_values_are_rejected() -> None:
    with pytest.raises(ValidationFailedError, match="bytes"):
        validate_fact_value("x" * 20_000)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1, Decimal("1.000")),
        (0.95, Decimal("0.950")),
        ("0.1234", Decimal("0.123")),
        (0, Decimal("0.000")),
    ],
)
def test_confidence_is_quantised(raw: float | str, expected: Decimal) -> None:
    assert normalize_confidence(raw) == expected


@pytest.mark.parametrize("raw", [-0.1, 1.01, "high"])
def test_invalid_confidence_is_rejected(raw: float | str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_confidence(raw)


@pytest.mark.parametrize("raw", [-1, 101, True])
def test_invalid_authority_is_rejected(raw: int) -> None:
    with pytest.raises(ValidationFailedError):
        validate_authority(raw)


def test_uri_allows_https_and_s3_only() -> None:
    assert normalize_uri(" https://crm.example.com/api ") == "https://crm.example.com/api"
    assert normalize_uri("s3://bucket/key.pdf") == "s3://bucket/key.pdf"
    assert normalize_uri(None) is None
    with pytest.raises(ValidationFailedError):
        normalize_uri("file:///etc/passwd")
