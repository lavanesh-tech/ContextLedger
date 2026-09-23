"""Context snapshots, decisions and decision facts (decision receipts).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("context_snapshots", "context_snapshot_facts", "decisions", "decision_facts")


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="RESTRICT",
    )


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["users.id"], name=op.f(f"fk_{table}_{column}_users"), ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("captured_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("query", sa.String(length=1000), nullable=False),
        sa.Column("valid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("vector_search", sa.String(length=16), nullable=False),
        sa.Column("privacy_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_snapshots")),
        _org_fk("context_snapshots"),
        _user_fk("context_snapshots", "captured_by_user_id"),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_context_snapshots_organization_id_id")
        ),
        sa.CheckConstraint(
            "vector_search IN ('used', 'unavailable')",
            name=op.f("ck_context_snapshots_vector_search_valid"),
        ),
    )
    op.create_index(
        "ix_context_snapshots_organization_id_created_at",
        "context_snapshots",
        ["organization_id", "created_at"],
    )

    op.create_table(
        "context_snapshot_facts",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ranking", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "fact_version_id", name=op.f("pk_context_snapshot_facts")
        ),
        _org_fk("context_snapshot_facts"),
        sa.ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["context_snapshots.organization_id", "context_snapshots.id"],
            name=op.f("fk_context_snapshot_facts_organization_id_context_snapshots"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            name=op.f("fk_context_snapshot_facts_organization_id_fact_versions"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "snapshot_id", "position", name=op.f("uq_context_snapshot_facts_snapshot_id_position")
        ),
        sa.CheckConstraint(
            "position >= 1", name=op.f("ck_context_snapshot_facts_position_positive")
        ),
    )
    op.create_index(
        "ix_context_snapshot_facts_organization_id_fact_version_id",
        "context_snapshot_facts",
        ["organization_id", "fact_version_id"],
    )

    op.create_table(
        "decisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("agent", sa.String(length=200), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale", sa.String(length=4000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decisions")),
        _org_fk("decisions"),
        _user_fk("decisions", "decided_by_user_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["context_snapshots.organization_id", "context_snapshots.id"],
            name=op.f("fk_decisions_organization_id_context_snapshots"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "snapshot_id",
            name=op.f("uq_decisions_organization_id_id_snapshot_id"),
        ),
        sa.CheckConstraint(
            r"action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'",
            name=op.f("ck_decisions_action_format"),
        ),
        sa.CheckConstraint(
            "receipt_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_decisions_receipt_sha256_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(outcome) <> 'null'", name=op.f("ck_decisions_outcome_not_null")
        ),
    )
    op.create_index(
        "ix_decisions_organization_id_decided_at", "decisions", ["organization_id", "decided_at"]
    )
    op.create_index(
        "ix_decisions_organization_id_snapshot_id", "decisions", ["organization_id", "snapshot_id"]
    )

    op.create_table(
        "decision_facts",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", "fact_version_id", name=op.f("pk_decision_facts")),
        _org_fk("decision_facts"),
        sa.ForeignKeyConstraint(
            ["organization_id", "decision_id", "snapshot_id"],
            ["decisions.organization_id", "decisions.id", "decisions.snapshot_id"],
            name=op.f("fk_decision_facts_organization_id_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "fact_version_id"],
            ["context_snapshot_facts.snapshot_id", "context_snapshot_facts.fact_version_id"],
            name=op.f("fk_decision_facts_snapshot_id_context_snapshot_facts"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_decision_facts_organization_id_fact_version_id",
        "decision_facts",
        ["organization_id", "fact_version_id"],
    )

    for table in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION contextledger_forbid_modification()
            """
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.drop_index("ix_decision_facts_organization_id_fact_version_id", table_name="decision_facts")
    op.drop_table("decision_facts")
    op.drop_index("ix_decisions_organization_id_snapshot_id", table_name="decisions")
    op.drop_index("ix_decisions_organization_id_decided_at", table_name="decisions")
    op.drop_table("decisions")
    op.drop_index(
        "ix_context_snapshot_facts_organization_id_fact_version_id",
        table_name="context_snapshot_facts",
    )
    op.drop_table("context_snapshot_facts")
    op.drop_index("ix_context_snapshots_organization_id_created_at", table_name="context_snapshots")
    op.drop_table("context_snapshots")
