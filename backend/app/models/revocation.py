"""Fact revocations and their impact on decisions.

A revocation says "this fact version was wrong", which is different from being
superseded ("this was true, and then it changed"). Both tables are append-only
(migration 0013): a revocation, and the list of decisions it affected at that
moment, are part of the audit record.

Effect on reads (``app/repositories/temporal.valid_at_condition``): a version
revoked at R is excluded from "valid at T as known at K" whenever K >= R (or K
is "latest"). Asked as known before R, it still appears, because that is what
was known then. Decision receipts keep showing it, marked with ``revoked_at``.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantOwnedMixin, UUIDPrimaryKeyMixin


class FactRevocation(UUIDPrimaryKeyMixin, TenantOwnedMixin, Base):
    __tablename__ = "fact_revocations"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "fact_version_id"),
        UniqueConstraint("organization_id", "id"),
        Index("ix_fact_revocations_organization_id_revoked_at", "organization_id", "revoked_at"),
        CheckConstraint("length(btrim(reason)) > 0", name="reason_present"),
    )

    fact_version_id: Mapped[uuid.UUID]
    reason: Mapped[str] = mapped_column(String(2000))
    revoked_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    agent_client_id: Mapped[uuid.UUID | None]
    revoked_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class RevocationImpact(TenantOwnedMixin, Base):
    """One decision whose frozen context contained the revoked version."""

    __tablename__ = "revocation_impacts"
    __table_args__ = (
        PrimaryKeyConstraint("revocation_id", "decision_id"),
        ForeignKeyConstraint(
            ["organization_id", "revocation_id"],
            ["fact_revocations.organization_id", "fact_revocations.id"],
            ondelete="RESTRICT",
        ),
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
        Index(
            "ix_revocation_impacts_organization_id_decision_id", "organization_id", "decision_id"
        ),
    )

    revocation_id: Mapped[uuid.UUID]
    decision_id: Mapped[uuid.UUID]
    snapshot_id: Mapped[uuid.UUID]
    fact_version_id: Mapped[uuid.UUID]
    relied_on: Mapped[bool]  # cited by the decision, not just present in its context
