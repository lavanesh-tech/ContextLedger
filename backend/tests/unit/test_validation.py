import pytest

from app.domain.errors import ValidationFailedError
from app.domain.validation import normalize_email, normalize_name, normalize_slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("acme", "acme"), ("  Acme-Corp ", "acme-corp"), ("a1b", "a1b"), ("x" * 63, "x" * 63)],
)
def test_valid_slugs_are_normalised(raw: str, expected: str) -> None:
    assert normalize_slug(raw) == expected


@pytest.mark.parametrize("raw", ["ab", "-acme", "acme-", "ac me", "acme_corp", "x" * 64, "ácme"])
def test_invalid_slugs_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_slug(raw)


def test_emails_are_trimmed_and_lower_cased() -> None:
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"


@pytest.mark.parametrize("raw", ["", "alice", "alice@", "@example.com", "a b@example.com"])
def test_invalid_emails_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_email(raw)


def test_overlong_emails_are_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        normalize_email("a" * 310 + "@example.com")


def test_names_collapse_whitespace() -> None:
    assert normalize_name("  Acme   Corp ", field="name") == "Acme Corp"


@pytest.mark.parametrize("raw", ["", "   ", "x" * 201])
def test_invalid_names_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_name(raw, field="name")
