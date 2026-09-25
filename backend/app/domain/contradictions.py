"""Contradiction rules (pure functions, no I/O).

ContextLedger keeps one version chain per fact: a newer version supersedes the
previous one. Usually that is an ordinary update. It is a *contradiction* when
the two versions cannot both be right:

Rule ``observed-value-conflict-v1``. A new version N supersedes a version P when

* N and P come from different sources,
* their values differ, and
* P's source directly observed its value at an instant N now covers
  (``N.valid_from <= P.observed_at``).

Example: the CRM observed ``customer_status = ACTIVE`` at 12:00; billing then
reports ``SUSPENDED`` valid from 10:00. At 12:00 the two sources disagree.

An ordinary update (a new value from a later instant than anything observed)
is not flagged, and neither is a correction from the same source. Nothing is
discarded: both versions stay in the history, and the contradiction records
which one the rules prefer (higher authority, then confidence, then the later
observation) without changing any data.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

RULE_OBSERVED_VALUE_CONFLICT = "observed-value-conflict-v1"


class ContradictionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ContradictionKind(StrEnum):
    VALUE_CONFLICT = "value_conflict"  # same fact, deterministic rule
    SEMANTIC = "semantic"  # different facts, suggested by an LLM review


@dataclass(frozen=True, slots=True)
class VersionFacts:
    """What the rules need to know about one version."""

    id: UUID
    source_id: UUID
    value: Any
    valid_from: datetime
    valid_until: datetime | None
    observed_at: datetime
    authority: int
    confidence: Decimal


def value_conflict(previous: VersionFacts, new: VersionFacts) -> str | None:
    """The reason ``new`` contradicts ``previous``, or None."""
    if previous.source_id == new.source_id or previous.value == new.value:
        return None
    if previous.valid_until is not None and previous.observed_at >= previous.valid_until:
        return None  # observed after its own validity ended: not a claim about that instant
    if new.valid_from > previous.observed_at:
        return None  # an ordinary update after the last observation
    return (
        f"source {previous.source_id} observed a different value at "
        f"{previous.observed_at.isoformat()}, which the new version (valid from "
        f"{new.valid_from.isoformat()}) also covers"
    )


def preferred(a: VersionFacts, b: VersionFacts) -> UUID:
    """The version the rules prefer: higher authority, then confidence, then later observation."""
    return max((a, b), key=lambda v: (v.authority, v.confidence, v.observed_at)).id
