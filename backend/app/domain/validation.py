"""Normalisation and validation of identifiers used across the domain.

The same rules are enforced again by PostgreSQL CHECK constraints, so data
written by any path (a service, a migration, a manual fix) stays valid.
"""

import re
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
