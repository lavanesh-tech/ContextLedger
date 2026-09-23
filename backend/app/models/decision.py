"""Context snapshots, decisions and decision receipts.

All four tables are append-only (trigger ``contextledger_forbid_modification``,
migration 0007). Composite foreign keys encode the rules in the database:

* every row stays inside one tenant;
* a snapshot fact is a real fact version of the same tenant;
* a decision references a snapshot of the same tenant;
* ``decision_facts`` references (decision, snapshot) *and* (snapshot, fact version),
  so a decision can only cite a fact that was in the context it was made with.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantOwnedMixin, UUIDPrimaryKeyMixin


class ContextSnapshot(UUIDPrimaryKeyMixin, TenantOwnedMixin, Base):
    """The retrieval context an actor had at one moment, frozen (``known_at`` is fixed)."""

    __tablename__ = "context_snapshots"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        Index("ix_context_snapshots_organization_id_created_at", "organization_id", "created_at"),
        CheckConstraint("vector_search IN ('used', 'unavailable')", name="vector_search_valid"),
    )

    captured_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    query: Mapped[str] = mapped_column(String(1000))
    valid_at: Mapped[datetime]
    known_at: Mapped[datetime]
    embedding_model: Mapped[str] = mapped_column(String(100))
    vector_search: Mapped[str] = mapped_column(String(16))
    privacy_scopes: Mapped[list[str]] = mapped_column(JSONB)
    # Limit, filters and trust weight: enough to explain (and re-run) the retrieval.
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class ContextSnapshotFact(TenantOwnedMixin, Base):
    """One fact version that was in a snapshot, with its rank and score breakdown."""

    __tablename__ = "context_snapshot_facts"
    __table_args__ = (
        PrimaryKeyConstraint("snapshot_id", "fact_version_id"),
        UniqueConstraint("snapshot_id", "position"),
        ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["context_snapshots.organization_id", "context_snapshots.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            ondelete="RESTRICT",
        ),
        # "Which snapshots (and so which decisions) contained this fact version?"
        Index(
            "ix_context_snapshot_facts_organization_id_fact_version_id",
            "organization_id",
            "fact_version_id",
        ),
        CheckConstraint("position >= 1", name="position_positive"),
    )

    snapshot_id: Mapped[uuid.UUID]
    fact_version_id: Mapped[uuid.UUID]
    position: Mapped[int] = mapped_column(Integer)
    ranking: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Decision(UUIDPrimaryKeyMixin, TenantOwnedMixin, Base):
    """What was decided, by whom, based on which snapshot; sealed by ``receipt_sha256``."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", "snapshot_id"),
        ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["context_snapshots.organization_id", "context_snapshots.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_decisions_organization_id_decided_at", "organization_id", "decided_at"),
        Index("ix_decisions_organization_id_snapshot_id", "organization_id", "snapshot_id"),
        CheckConstraint(r"action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'", name="action_format"),
        CheckConstraint("receipt_sha256 ~ '^[0-9a-f]{64}$'", name="receipt_sha256_format"),
        CheckConstraint("jsonb_typeof(outcome) <> 'null'", name="outcome_not_null"),
    )

    snapshot_id: Mapped[uuid.UUID]
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    agent: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[Any] = mapped_column(JSONB)
    rationale: Mapped[str | None] = mapped_column(String(4000))
    decided_at: Mapped[datetime]
    receipt_sha256: Mapped[str] = mapped_column(String(64))


class DecisionFact(TenantOwnedMixin, Base):
    """A fact version the decision relied on (always one that was in its snapshot)."""

    __tablename__ = "decision_facts"
    __table_args__ = (
        PrimaryKeyConstraint("decision_id", "fact_version_id"),
        ForeignKeyConstraint(
            ["organization_id", "decision_id", "snapshot_id"],
            ["decisions.organization_id", "decisions.id", "decisions.snapshot_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "fact_version_id"],
            ["context_snapshot_facts.snapshot_id", "context_snapshot_facts.fact_version_id"],
            ondelete="RESTRICT",
        ),
        # "Which decisions relied on this fact version?" (revocation impact, Phase 17)
        Index(
            "ix_decision_facts_organization_id_fact_version_id",
            "organization_id",
            "fact_version_id",
        ),
    )

    decision_id: Mapped[uuid.UUID]
    snapshot_id: Mapped[uuid.UUID]
    fact_version_id: Mapped[uuid.UUID]
