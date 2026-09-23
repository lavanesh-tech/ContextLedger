import hashlib

import pytest

from app.domain.errors import ValidationFailedError
from app.domain.evidence import (
    EXCERPT_MAX_CHARS,
    content_hash,
    normalize_excerpt,
    validate_metadata,
)


def test_excerpt_normalisation_unifies_line_endings_and_trims() -> None:
    assert normalize_excerpt("  limit:\r\n2000\rUSD  ") == "limit:\n2000\nUSD"


def test_inner_whitespace_is_preserved() -> None:
    assert normalize_excerpt("a  b\n\n c") == "a  b\n\n c"


@pytest.mark.parametrize("raw", ["", "   ", "\r\n", "x" * (EXCERPT_MAX_CHARS + 1), "bad\x00byte"])
def test_invalid_excerpts_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_excerpt(raw)


def test_hash_is_sha256_of_utf8_normalised_text() -> None:
    text = normalize_excerpt("Crédit limit: 2000 €")

    assert content_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(content_hash(text)) == 64


def test_same_content_from_different_platforms_hashes_identically() -> None:
    windows = normalize_excerpt("line one\r\nline two\r\n")
    unix = normalize_excerpt("line one\nline two\n")

    assert content_hash(windows) == content_hash(unix)


def test_different_content_hashes_differently() -> None:
    assert content_hash("limit 2000") != content_hash("limit 5000")


def test_metadata_defaults_to_empty_object() -> None:
    assert validate_metadata(None) == {}


def test_metadata_accepts_small_json_objects() -> None:
    assert validate_metadata({"page": 3, "http_status": 200}) == {"page": 3, "http_status": 200}


@pytest.mark.parametrize(
    "value", [[1, 2], "text", {"x": float("nan")}, {"x": {1, 2}}, {"blob": "x" * 5000}]
)
def test_invalid_metadata_is_rejected(value: object) -> None:
    with pytest.raises(ValidationFailedError):
        validate_metadata(value)  # type: ignore[arg-type]
