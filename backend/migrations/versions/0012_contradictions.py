"""Contradictions between fact versions, and the contradiction.detected event.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_COLUMNS = (
    "id",
    "entity_id",
    "left_version_id",
    "right_version_id",
    "kind",
    "detector",
    "privacy_scope",
    "detected_at",
)


def _version_fk(column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", column],
        ["fact_versions.organization_id", "fact_versions.id"],
        name=op.f(f"fk_contradictions_{column.removesuffix('_version_id')}_version"),
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "contradictions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("left_version_id", sa.Uuid(), nullable=False),
        sa.Column("right_version_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("detector", sa.String(length=64), nullable=False),
        sa.Column("explanation", sa.String(length=2000), nullable=False),
        sa.Column("preferred_version_id", sa.Uuid(), nullable=True),
        sa.Column("privacy_scope", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolution_note", sa.String(length=2000), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contradictions")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_contradictions_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "entity_id"],
            ["entities.organization_id", "entities.id"],
            name=op.f("fk_contradictions_organization_id_entities"),
            ondelete="RESTRICT",
        ),
        _version_fk("left_version_id"),
        _version_fk("right_version_id"),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"],
            ["users.id"],
            name=op.f("fk_contradictions_resolved_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "left_version_id",
            "right_version_id",
            name=op.f("uq_contradictions_version_pair"),
        ),
        sa.CheckConstraint(
            "left_version_id <> right_version_id", name=op.f("ck_contradictions_distinct_versions")
        ),
        sa.CheckConstraint(
            "kind IN ('value_conflict', 'semantic')", name=op.f("ck_contradictions_kind_valid")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')",
            name=op.f("ck_contradictions_status_valid"),
        ),
        sa.CheckConstraint(
            "privacy_scope IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name=op.f("ck_contradictions_privacy_scope_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'open') = (resolved_at IS NULL)",
            name=op.f("ck_contradictions_resolution_consistent"),
        ),
    )
    op.create_index(
        "ix_contradictions_organization_id_status_detected_at",
        "contradictions",
        ["organization_id", "status", "detected_at"],
    )
    op.create_index(
        "ix_contradictions_organization_id_entity_id",
        "contradictions",
        ["organization_id", "entity_id"],
    )
    args = ", ".join(f"'{value}'" for value in ("contradiction.detected", "id", *EVENT_COLUMNS))
    op.execute(
        f"""
        CREATE TRIGGER trg_contradictions_emit_event
        AFTER INSERT ON contradictions
        FOR EACH ROW EXECUTE FUNCTION contextledger_emit_event({args})
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_contradictions_emit_event ON contradictions")
    op.drop_index("ix_contradictions_organization_id_entity_id", table_name="contradictions")
    op.drop_index(
        "ix_contradictions_organization_id_status_detected_at", table_name="contradictions"
    )
    op.drop_table("contradictions")
