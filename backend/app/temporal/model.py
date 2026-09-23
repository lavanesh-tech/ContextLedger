"""Value types of the temporal resolution engine (framework-free)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.facts import PrivacyScope


@dataclass(frozen=True, slots=True)
class VersionSnapshot:
    """An immutable copy of one fact version, as seen at some point in transaction time.

    When a snapshot is taken "as known at K" and the version's end had not been
    learned yet at K, ``valid_until`` and ``valid_until_recorded_at`` are None:
    at that moment the version looked open-ended.
    """

    id: UUID
    fact_id: UUID
    version: int
    value: Any
    source_id: UUID
    valid_from: datetime
    valid_until: datetime | None
    observed_at: datetime
    recorded_at: datetime
    valid_until_recorded_at: datetime | None
    supersedes_id: UUID | None
    authority: int
    confidence: Decimal
    privacy_scope: PrivacyScope


@dataclass(frozen=True, slots=True)
class ResolvedFact:
    """The version of one fact that answers a temporal question, with its identity."""

    fact_id: UUID
    entity_id: UUID
    entity_type: str
    external_id: str
    property: str
    version: VersionSnapshot


@dataclass(frozen=True, slots=True)
class FactChange:
    """How one fact changed in valid time between ``start`` and ``end``."""

    fact_id: UUID
    property: str
    before: VersionSnapshot | None  # the version valid at start (None: no value)
    after: VersionSnapshot | None  # the version valid at end (None: no value)
    transitions: tuple[VersionSnapshot, ...]  # versions taking effect in (start, end]


@dataclass(frozen=True, slots=True)
class Lineage:
    version: VersionSnapshot
    ancestors: tuple[VersionSnapshot, ...]  # oldest first
    descendants: tuple[VersionSnapshot, ...]  # oldest first

    @property
    def superseded_by(self) -> VersionSnapshot | None:
        return self.descendants[0] if self.descendants else None


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    property: str
    version: VersionSnapshot
