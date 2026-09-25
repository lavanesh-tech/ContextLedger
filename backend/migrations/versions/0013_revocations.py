"""Fact revocations and the decisions they impact.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("fact_revocations", "revocation_impacts")
# table -> (event type, aggregate-id column, payload columns); identifiers only.
EVENTS = {
    "fact_revocations": (
        "fact.revoked",
        "fact_version_id",
        ("id", "fact_version_id", "revoked_by_user_id", "revoked_at"),
    ),
    "revocation_impacts": (
        "decision.impacted",
        "decision_id",
        ("revocation_id", "decision_id", "snapshot_id", "fact_version_id", "relied_on"),
    ),
}


def upgrade() -> None:
    op.create_table(
        "fact_revocations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_client_id", sa.Uuid(), nullable=True),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fact_revocations")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_fact_revocations_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            name=op.f("fk_fact_revocations_organization_id_fact_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
            name=op.f("fk_fact_revocations_revoked_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        # A version is revoked at most once.
        sa.UniqueConstraint(
            "organization_id",
            "fact_version_id",
            name=op.f("uq_fact_revocations_organization_id_fact_version_id"),
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_fact_revocations_organization_id_id")
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name=op.f("ck_fact_revocations_reason_present")
        ),
    )
    op.create_index(
        "ix_fact_revocations_organization_id_revoked_at",
        "fact_revocations",
        ["organization_id", "revoked_at"],
    )

    op.create_table(
        "revocation_impacts",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("revocation_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.Column("relied_on", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("revocation_id", "decision_id", name=op.f("pk_revocation_impacts")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_revocation_impacts_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "revocation_id"],
            ["fact_revocations.organization_id", "fact_revocations.id"],
            name=op.f("fk_revocation_impacts_organization_id_fact_revocations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "decision_id", "snapshot_id"],
            ["decisions.organization_id", "decisions.id", "decisions.snapshot_id"],
            name=op.f("fk_revocation_impacts_organization_id_decisions"),
            ondelete="RESTRICT",
        ),
        # The revoked version really was in the decision's context.
        sa.ForeignKeyConstraint(
            ["snapshot_id", "fact_version_id"],
            ["context_snapshot_facts.snapshot_id", "context_snapshot_facts.fact_version_id"],
            name=op.f("fk_revocation_impacts_snapshot_id_context_snapshot_facts"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_revocation_impacts_organization_id_decision_id",
        "revocation_impacts",
        ["organization_id", "decision_id"],
    )

    for table in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION contextledger_forbid_modification()
            """
        )
    for table, (event_type, aggregate, columns) in EVENTS.items():
        args = ", ".join(f"'{value}'" for value in (event_type, aggregate, *columns))
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_emit_event
            AFTER INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION contextledger_emit_event({args})
            """
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_emit_event ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.drop_index(
        "ix_revocation_impacts_organization_id_decision_id", table_name="revocation_impacts"
    )
    op.drop_table("revocation_impacts")
    op.drop_index("ix_fact_revocations_organization_id_revoked_at", table_name="fact_revocations")
    op.drop_table("fact_revocations")
