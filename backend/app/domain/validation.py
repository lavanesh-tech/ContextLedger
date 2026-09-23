"""Normalisation and validation of identifiers used across the domain.

The same rules are enforced again by PostgreSQL CHECK constraints, so data
written by any path (a service, a migration, a manual fix) stays valid.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Final

from app.domain.errors import ValidationFailedError

# 3-63 chars, lowercase letters/digits/hyphens, no leading/trailing hyphen.
# Mirrors ck_organizations_slug_format in migration 0002.
SLUG_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

# Deliberately simple: real verification happens by e-mail/OAuth, not by regex.
_EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX_LENGTH: Final = 320
NAME_MAX_LENGTH: Final = 200


def normalize_slug(value: str) -> str:
    slug = value.strip().lower()
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValidationFailedError(
            "slug must be 3-63 characters of a-z, 0-9 or '-', and must not start or end with '-'"
        )
    return slug


def normalize_email(value: str) -> str:
    """Emails are stored lower-cased so uniqueness is case-insensitive."""
    email = value.strip().lower()
    if len(email) > EMAIL_MAX_LENGTH or not _EMAIL_PATTERN.fullmatch(email):
        raise ValidationFailedError("email address is not valid")
    return email


def normalize_name(value: str, *, field: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise ValidationFailedError(f"{field} must not be empty")
    if len(name) > NAME_MAX_LENGTH:
        raise ValidationFailedError(f"{field} must be at most {NAME_MAX_LENGTH} characters")
    return name


# --- Temporal fact identifiers and values ----------------------------------------

# Lower snake_case segments separated by dots, e.g. "credit_limit" or
# "billing.credit_limit". Mirrors the *_format CHECK constraints in migration 0003.
IDENTIFIER_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
IDENTIFIER_MAX_LENGTH: Final = 128
EXTERNAL_ID_MAX_LENGTH: Final = 256
URI_MAX_LENGTH: Final = 2048
FACT_VALUE_MAX_BYTES: Final = 16_384
_ALLOWED_URI_SCHEMES: Final = ("https://", "http://", "s3://")
_CONFIDENCE_STEP: Final = Decimal("0.001")


def normalize_identifier(value: str, *, field: str) -> str:
    identifier = value.strip().lower()
    if len(identifier) > IDENTIFIER_MAX_LENGTH or not IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValidationFailedError(
            f"{field} must be lower snake_case segments separated by '.', "
            f"at most {IDENTIFIER_MAX_LENGTH} characters"
        )
    return identifier


def normalize_external_id(value: str) -> str:
    external_id = value.strip()
    if not external_id or len(external_id) > EXTERNAL_ID_MAX_LENGTH:
        raise ValidationFailedError(f"external_id must be 1-{EXTERNAL_ID_MAX_LENGTH} characters")
    if any(not ch.isprintable() for ch in external_id):
        raise ValidationFailedError("external_id must not contain control characters")
    return external_id


def validate_fact_value(value: object) -> object:
    """Fact values are JSON (number, string, bool, array or object), never null."""
    if value is None:
        raise ValidationFailedError("value must not be null; model 'unknown' explicitly")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError(f"value is not valid JSON: {exc}") from exc
    if len(encoded.encode()) > FACT_VALUE_MAX_BYTES:
        raise ValidationFailedError(f"value must be at most {FACT_VALUE_MAX_BYTES} bytes as JSON")
    return value


def normalize_confidence(value: Decimal | float | str) -> Decimal:
    try:
        confidence = Decimal(str(value)).quantize(_CONFIDENCE_STEP)
    except InvalidOperation as exc:
        raise ValidationFailedError("confidence must be a number") from exc
    if not Decimal(0) <= confidence <= Decimal(1):
        raise ValidationFailedError("confidence must be between 0 and 1")
    return confidence


def validate_authority(value: int) -> int:
    if isinstance(value, bool) or not 0 <= value <= 100:
        raise ValidationFailedError("authority must be an integer between 0 and 100")
    return value


def normalize_uri(value: str | None) -> str | None:
    if value is None:
        return None
    uri = value.strip()
    if len(uri) > URI_MAX_LENGTH or not uri.startswith(_ALLOWED_URI_SCHEMES):
        raise ValidationFailedError(
            "uri must be an http(s):// or s3:// URL of at most 2048 characters"
        )
    return uri
