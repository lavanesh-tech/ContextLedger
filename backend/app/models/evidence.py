"""Evidence and its links to fact versions. Both tables are immutable (migration 0004)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import StringEnum
from app.domain.evidence import EvidenceRelation, EvidenceType
from app.domain.facts import PrivacyScope
from app.models.mixins import TenantOwnedMixin, UUIDPrimaryKeyMixin


class Evidence(UUIDPrimaryKeyMixin, TenantOwnedMixin, Base):
    __tablename__ = "evidence"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "source_id"],
            ["fact_sources.organization_id", "fact_sources.id"],
            ondelete="RESTRICT",
        ),
        # Content-addressed: the same content from the same source is stored once.
        UniqueConstraint("organization_id", "source_id", "content_sha256"),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            "evidence_type IN ('DOCUMENT_EXCERPT', 'API_RESPONSE', 'DATABASE_RECORD', "
            "'HUMAN_STATEMENT', 'AGENT_OUTPUT')",
            name="evidence_type_valid",
        ),
        CheckConstraint("char_length(excerpt) BETWEEN 1 AND 8000", name="excerpt_length"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_format"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_is_object"),
        CheckConstraint(
            "privacy_scope IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name="privacy_scope_valid",
        ),
    )

    source_id: Mapped[uuid.UUID]
    evidence_type: Mapped[EvidenceType] = mapped_column(StringEnum(EvidenceType, length=32))
    excerpt: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    uri: Mapped[str | None] = mapped_column(String(2048))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB)
    privacy_scope: Mapped[PrivacyScope] = mapped_column(StringEnum(PrivacyScope, length=16))
    captured_at: Mapped[datetime]  # when the source produced / we captured the material
    recorded_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class FactVersionEvidence(TenantOwnedMixin, Base):
    """Evidence -[SUPPORTS | CONTRADICTS]-> FactVersion. Append-only."""

    __tablename__ = "fact_version_evidence"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        PrimaryKeyConstraint("fact_version_id", "evidence_id"),
        ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "evidence_id"],
            ["evidence.organization_id", "evidence.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("relation IN ('SUPPORTS', 'CONTRADICTS')", name="relation_valid"),
    )

    fact_version_id: Mapped[uuid.UUID]
    # "Which versions does this evidence back?" (revocation impact, Phase 17).
    evidence_id: Mapped[uuid.UUID] = mapped_column(index=True)
    relation: Mapped[EvidenceRelation] = mapped_column(StringEnum(EvidenceRelation, length=16))
    linked_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    linked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
