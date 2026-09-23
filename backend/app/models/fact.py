"""Temporal facts. See app/domain/facts.py for the rules these tables encode.

Database-level guarantees (migration 0003):
* Composite foreign keys keep every reference inside one tenant.
* ``ex_fact_versions_no_overlapping_validity`` (GiST exclusion constraint):
  two versions of the same fact can never be valid at the same instant.
* A trigger makes fact_versions append-only: rows cannot be deleted, and the
  only permitted update is closing an open version once (``valid_until`` and
  ``valid_until_recorded_at``).
* ``uq_fact_versions_supersedes_id``: a version is superseded at most once, so
  lineage is a single chain.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import StringEnum
from app.domain.facts import PrivacyScope
from app.models.mixins import TenantOwnedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Fact(UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, Base):
    """The identity of one property of one entity, e.g. customer-991 / credit_limit."""

    __tablename__ = "facts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "entity_id"],
            ["entities.organization_id", "entities.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "entity_id", "property"),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            r"property ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'", name="property_format"
        ),
    )

    entity_id: Mapped[uuid.UUID]
    property: Mapped[str] = mapped_column(String(128))


class FactVersion(UUIDPrimaryKeyMixin, TenantOwnedMixin, Base):
    """One immutable value of a fact, with valid time and transaction time."""

    __tablename__ = "fact_versions"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "fact_id"],
            ["facts.organization_id", "facts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "source_id"],
            ["fact_sources.organization_id", "fact_sources.id"],
            ondelete="RESTRICT",
        ),
        # A version can only supersede a version of the *same* fact.
        ForeignKeyConstraint(
            ["fact_id", "supersedes_id"],
            ["fact_versions.fact_id", "fact_versions.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("fact_id", "version"),
        UniqueConstraint("fact_id", "id"),
        UniqueConstraint("supersedes_id"),
        Index(
            "ix_fact_versions_organization_id_fact_id_valid_from",
            "organization_id",
            "fact_id",
            "valid_from",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("(version = 1) = (supersedes_id IS NULL)", name="lineage_consistent"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from", name="valid_range_ordered"
        ),
        CheckConstraint(
            "(valid_until IS NULL) = (valid_until_recorded_at IS NULL)",
            name="valid_until_recorded_together",
        ),
        CheckConstraint("authority BETWEEN 0 AND 100", name="authority_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "privacy_scope IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name="privacy_scope_valid",
        ),
        CheckConstraint("jsonb_typeof(value) <> 'null'", name="value_not_null"),
    )

    fact_id: Mapped[uuid.UUID]
    version: Mapped[int] = mapped_column(Integer)
    value: Mapped[Any] = mapped_column(JSONB)
    source_id: Mapped[uuid.UUID]

    # Valid time: when the value is true in the real world, [valid_from, valid_until).
    valid_from: Mapped[datetime]
    valid_until: Mapped[datetime | None]
    # When the source observed the value (may differ from valid_from).
    observed_at: Mapped[datetime]

    # Transaction time: when ContextLedger learned this version, and when it
    # learned that the version ended. Both come from the database clock.
    recorded_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    valid_until_recorded_at: Mapped[datetime | None]

    supersedes_id: Mapped[uuid.UUID | None]
    authority: Mapped[int] = mapped_column(SmallInteger)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    privacy_scope: Mapped[PrivacyScope] = mapped_column(StringEnum(PrivacyScope, length=16))
