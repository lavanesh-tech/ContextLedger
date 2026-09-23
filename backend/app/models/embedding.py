"""Vector embeddings of fact versions, and the job queue that produces them."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import StringEnum
from app.db.vector import Vector
from app.domain.embeddings import EMBEDDING_DIMENSIONS, EmbeddingJobStatus
from app.models.mixins import TenantOwnedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class FactEmbedding(TenantOwnedMixin, Base):
    """One embedding of one fact version under one model.

    Keyed by (fact_version_id, model) so a model upgrade can re-embed alongside
    the old vectors and switch over without downtime. The HNSW index
    ``ann_fact_embeddings_embedding_cosine`` is defined in migration 0005.
    """

    __tablename__ = "fact_embeddings"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        PrimaryKeyConstraint("fact_version_id", "model"),
        ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            ondelete="RESTRICT",
        ),
        # Cache lookups: "has this org already embedded this exact text with this model?"
        Index(
            "ix_fact_embeddings_organization_id_model_content_sha256",
            "organization_id",
            "model",
            "content_sha256",
        ),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_format"),
        CheckConstraint(f"dimensions = {EMBEDDING_DIMENSIONS}", name="dimensions_supported"),
    )

    fact_version_id: Mapped[uuid.UUID]
    model: Mapped[str] = mapped_column(String(100))
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    text_template: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class EmbeddingJob(UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, Base):
    """Work item: embed one fact version with one model.

    Claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``. ``lease_token`` identifies
    the claim, so a worker whose lease expired (and whose job was re-claimed by
    another worker) cannot overwrite the newer outcome.
    """

    __tablename__ = "embedding_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("fact_version_id", "model"),
        Index("ix_embedding_jobs_model_status_available_at", "model", "status", "available_at"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')", name="status_valid"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "(status = 'RUNNING') = (lease_token IS NOT NULL)", name="lease_only_while_running"
        ),
    )

    fact_version_id: Mapped[uuid.UUID]
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[EmbeddingJobStatus] = mapped_column(StringEnum(EmbeddingJobStatus, length=16))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    locked_at: Mapped[datetime | None]
    lease_token: Mapped[uuid.UUID | None]
    last_error: Mapped[str | None] = mapped_column(String(500))
