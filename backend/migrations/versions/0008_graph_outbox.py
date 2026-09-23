"""Transactional outbox for the Neo4j provenance graph, filled by triggers.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Table -> primary-key columns the projector needs to re-read the row.
PROJECTED_TABLES: dict[str, tuple[str, ...]] = {
    "entities": ("id",),
    "facts": ("id",),
    "fact_sources": ("id",),
    "fact_versions": ("id",),
    "evidence": ("id",),
    "fact_version_evidence": ("fact_version_id", "evidence_id"),
    "context_snapshots": ("id",),
    "context_snapshot_facts": ("snapshot_id", "fact_version_id"),
    "decisions": ("id",),
    "decision_facts": ("decision_id", "fact_version_id"),
}

OUTBOX_FUNCTION = """
CREATE FUNCTION contextledger_graph_outbox() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    payload jsonb := to_jsonb(NEW);
    key jsonb := '{}'::jsonb;
    col text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        key := key || jsonb_build_object(col, payload -> col);
    END LOOP;
    INSERT INTO graph_outbox (organization_id, table_name, row_key)
    VALUES (NEW.organization_id, TG_TABLE_NAME, key);
    RETURN NULL;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "graph_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_name", sa.String(length=64), nullable=False),
        sa.Column("row_key", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_outbox")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_graph_outbox_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_graph_outbox_organization_id_id", "graph_outbox", ["organization_id", "id"])
    op.execute(OUTBOX_FUNCTION)
    for table, key in PROJECTED_TABLES.items():
        args = ", ".join(f"'{column}'" for column in key)
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_graph_outbox
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION contextledger_graph_outbox({args})
            """
        )
        # Backfill: project everything that already exists.
        key_json = ", ".join(f"'{column}', {column}" for column in key)
        op.execute(
            f"""
            INSERT INTO graph_outbox (organization_id, table_name, row_key)
            SELECT organization_id, '{table}', jsonb_build_object({key_json}) FROM {table}
            """  # noqa: S608 (constant table and column names)
        )


def downgrade() -> None:
    for table in PROJECTED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_graph_outbox ON {table}")
    op.execute("DROP FUNCTION IF EXISTS contextledger_graph_outbox()")
    op.drop_index("ix_graph_outbox_organization_id_id", table_name="graph_outbox")
    op.drop_table("graph_outbox")
