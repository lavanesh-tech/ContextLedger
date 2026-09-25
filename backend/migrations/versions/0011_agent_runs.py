"""Agent runs: an immutable trace of every Historical Decision Investigator run.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_client_id", sa.Uuid(), nullable=True),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("question", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("cited_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rejected_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_runs_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name=op.f("fk_agent_runs_requested_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('answered', 'insufficient_evidence', 'ungrounded', 'step_limit', 'failed')",
            name=op.f("ck_agent_runs_status_valid"),
        ),
        sa.CheckConstraint(
            "steps >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name=op.f("ck_agent_runs_counters_non_negative"),
        ),
    )
    op.create_index(
        "ix_agent_runs_organization_id_created_at", "agent_runs", ["organization_id", "created_at"]
    )
    # A trace is evidence of what the agent did: append-only, like decision receipts.
    op.execute(
        """
        CREATE TRIGGER trg_agent_runs_immutable
        BEFORE UPDATE OR DELETE ON agent_runs
        FOR EACH ROW EXECUTE FUNCTION contextledger_forbid_modification()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agent_runs_immutable ON agent_runs")
    op.drop_index("ix_agent_runs_organization_id_created_at", table_name="agent_runs")
    op.drop_table("agent_runs")
