"""Temporal fact rules (framework-free).

Vocabulary
----------
* **Entity**: the thing a fact is about, e.g. ``customer / customer-991``.
* **Fact**: one property of one entity, e.g. ``credit_limit`` of customer-991.
  It never changes; it only accumulates versions.
* **FactVersion**: an immutable value of a fact with two timelines:

  - *valid time*, ``[valid_from, valid_until)``: when the value is true in the
    real world. ``valid_until = None`` means "until further notice".
  - *transaction time*, ``recorded_at``: when ContextLedger learned it. When a
    later version closes this one, ``valid_until_recorded_at`` records when
    that happened.

  Keeping both timelines (bitemporal data) is what lets ContextLedger answer
  "what did the agent know at 11:00?" correctly even after later corrections.

Supersession rules (deterministic)
----------------------------------
1. A fact's versions never overlap in valid time (also enforced by an
   exclusion constraint in PostgreSQL).
2. A new version must start strictly after the latest version started.
   Rewriting older history is a revocation (Phase 17), not an edit.
3. If the latest version is open-ended, it is closed at the new version's
   ``valid_from``. If it already has a fixed end, the new version must not
   start before that end (gaps are allowed, overlaps are not).
4. Version numbers are 1, 2, 3, ... per fact. Each version except the first
   supersedes exactly the previous one, so lineage is a single chain.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.domain.errors import ConflictError, ValidationFailedError


class SourceType(StrEnum):
    SYSTEM_OF_RECORD = "SYSTEM_OF_RECORD"  # e.g. the billing database
    API = "API"
    DOCUMENT = "DOCUMENT"
    HUMAN = "HUMAN"
    AGENT = "AGENT"


class PrivacyScope(StrEnum):
    """Who may see a fact version. Enforced in retrieval (Phases 8 and 13)."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


def require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailedError(f"{field} must include a timezone (e.g. 2026-09-23T10:30:00Z)")
    return value


@dataclass(frozen=True, slots=True)
class ValidityWindow:
    """Half-open interval ``[valid_from, valid_until)``; ``valid_until=None`` is open-ended."""

    valid_from: datetime
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        require_aware(self.valid_from, field="valid_from")
        if self.valid_until is not None:
            require_aware(self.valid_until, field="valid_until")
            if self.valid_until <= self.valid_from:
                raise ValidationFailedError("valid_until must be later than valid_from")

    def contains(self, instant: datetime) -> bool:
        require_aware(instant, field="instant")
        return self.valid_from <= instant and (
            self.valid_until is None or instant < self.valid_until
        )


@dataclass(frozen=True, slots=True)
class LatestVersion:
    version: int
    window: ValidityWindow


@dataclass(frozen=True, slots=True)
class VersionPlan:
    """What must happen to append a version: its number, and where to close the previous one."""

    version: int
    close_previous_at: datetime | None


def plan_new_version(latest: LatestVersion | None, window: ValidityWindow) -> VersionPlan:
    if latest is None:
        return VersionPlan(version=1, close_previous_at=None)

    previous = latest.window
    if window.valid_from <= previous.valid_from:
        raise ConflictError(
            f"new version must start after version {latest.version} "
            f"(valid_from {previous.valid_from.isoformat()}); "
            "rewriting history requires a revocation"
        )
    if previous.valid_until is None:
        return VersionPlan(version=latest.version + 1, close_previous_at=window.valid_from)
    if window.valid_from < previous.valid_until:
        raise ConflictError(
            f"new version overlaps version {latest.version}, which is valid until "
            f"{previous.valid_until.isoformat()}"
        )
    return VersionPlan(version=latest.version + 1, close_previous_at=None)
