"""Contradictions: pairs of fact versions that cannot both be right.

``value_conflict`` rows are written by the deterministic rule in
app/domain/contradictions.py, in the same transaction as the version that
caused them. ``semantic`` rows are suggestions from an LLM review of an entity,
validated against the versions that were actually supplied. Both versions
always stay in the history: a contradiction never deletes or hides data.

``privacy_scope`` is the more sensitive scope of the two versions, so a reader
who may not see either version does not see the contradiction.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import StringEnum
from app.domain.contradictions import ContradictionKind, ContradictionStatus
from app.domain.facts import PrivacyScope
from app.models.mixins import TenantOwnedMixin, UUIDPrimaryKeyMixin


class Contradiction(UUIDPrimaryKeyMixin, TenantOwnedMixin, Base):
    __tablename__ = "contradictions"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "entity_id"],
            ["entities.organization_id", "entities.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "left_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            name="fk_contradictions_left_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "right_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            name="fk_contradictions_right_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "left_version_id",
            "right_version_id",
            name="uq_contradictions_version_pair",
        ),
        Index(
            "ix_contradictions_organization_id_status_detected_at",
            "organization_id",
            "status",
            "detected_at",
        ),
        Index("ix_contradictions_organization_id_entity_id", "organization_id", "entity_id"),
        CheckConstraint("left_version_id <> right_version_id", name="distinct_versions"),
        CheckConstraint("kind IN ('value_conflict', 'semantic')", name="kind_valid"),
        CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name="status_valid"),
        CheckConstraint(
            "privacy_scope IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name="privacy_scope_valid",
        ),
        CheckConstraint("(status = 'open') = (resolved_at IS NULL)", name="resolution_consistent"),
    )

    entity_id: Mapped[uuid.UUID]
    left_version_id: Mapped[uuid.UUID]  # the earlier version
    right_version_id: Mapped[uuid.UUID]  # the later version
    kind: Mapped[ContradictionKind] = mapped_column(StringEnum(ContradictionKind, length=32))
    detector: Mapped[str] = mapped_column(String(64))  # rule id, or llm:<prompt version>
    explanation: Mapped[str] = mapped_column(String(2000))
    preferred_version_id: Mapped[uuid.UUID | None]
    privacy_scope: Mapped[PrivacyScope] = mapped_column(StringEnum(PrivacyScope, length=16))
    status: Mapped[ContradictionStatus] = mapped_column(
        StringEnum(ContradictionStatus, length=16), default=ContradictionStatus.OPEN
    )
    resolution_note: Mapped[str | None] = mapped_column(String(2000))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolved_at: Mapped[datetime | None]
    detected_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
