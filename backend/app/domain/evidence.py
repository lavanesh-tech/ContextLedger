"""Evidence rules (framework-free).

Evidence is the material a source produced to back a claim: a document excerpt,
an API response body, a person's statement, an agent's output. It is linked to
the exact fact version(s) it supports.

* Evidence is **immutable** and **content-addressed**: the SHA-256 of the
  normalised excerpt identifies it within its source, so capturing the same
  content twice returns the existing evidence (idempotent ingestion).
* Links between evidence and fact versions are **append-only**: provenance
  cannot be rewritten after the fact.
"""

import hashlib
import json
from enum import StrEnum
from typing import Any, Final

from app.domain.errors import ValidationFailedError

EXCERPT_MAX_CHARS: Final = 8_000
METADATA_MAX_BYTES: Final = 4_096


class EvidenceType(StrEnum):
    DOCUMENT_EXCERPT = "DOCUMENT_EXCERPT"
    API_RESPONSE = "API_RESPONSE"
    DATABASE_RECORD = "DATABASE_RECORD"
    HUMAN_STATEMENT = "HUMAN_STATEMENT"
    AGENT_OUTPUT = "AGENT_OUTPUT"


class EvidenceRelation(StrEnum):
    SUPPORTS = "SUPPORTS"  # the evidence backs the version's value
    CONTRADICTS = "CONTRADICTS"  # the evidence argues against it (used from Phase 16)


def normalize_excerpt(value: str) -> str:
    """Canonical form used for storage *and* hashing.

    Line endings become ``\\n`` and surrounding whitespace is removed, so the same
    text copied from Windows and Unix systems hashes identically. Inner content
    is kept exactly as provided.
    """
    excerpt = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not excerpt:
        raise ValidationFailedError("excerpt must not be empty")
    if len(excerpt) > EXCERPT_MAX_CHARS:
        raise ValidationFailedError(
            f"excerpt must be at most {EXCERPT_MAX_CHARS} characters; "
            "store larger documents externally and reference them by uri"
        )
    if "\x00" in excerpt:
        raise ValidationFailedError("excerpt must not contain NUL characters")
    return excerpt


def content_hash(normalized_excerpt: str) -> str:
    """Lower-case hex SHA-256 of the normalised excerpt (UTF-8)."""
    return hashlib.sha256(normalized_excerpt.encode("utf-8")).hexdigest()


def validate_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    """Small JSON object describing the capture (page number, HTTP status, ...)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationFailedError("metadata must be a JSON object")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError(f"metadata is not valid JSON: {exc}") from exc
    if len(encoded.encode()) > METADATA_MAX_BYTES:
        raise ValidationFailedError(f"metadata must be at most {METADATA_MAX_BYTES} bytes")
    return value
