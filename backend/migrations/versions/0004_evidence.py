"""Evidence and fact-version evidence links (both immutable).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("evidence", "fact_version_evidence")


def upgrade() -> None:
    # Composite target so evidence links can only point at versions of the same tenant.
    op.create_unique_constraint(
        op.f("uq_fact_versions_organization_id_id"), "fact_versions", ["organization_id", "id"]
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("uri", sa.String(length=2048), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("privacy_scope", sa.String(length=16), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_evidence_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_id"],
            ["fact_sources.organization_id", "fact_sources.id"],
            name=op.f("fk_evidence_organization_id_fact_sources"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "source_id",
            "content_sha256",
            name=op.f("uq_evidence_organization_id_source_id_content_sha256"),
        ),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_evidence_organization_id_id")),
        sa.CheckConstraint(
            "evidence_type IN ('DOCUMENT_EXCERPT', 'API_RESPONSE', 'DATABASE_RECORD', "
            "'HUMAN_STATEMENT', 'AGENT_OUTPUT')",
            name=op.f("ck_evidence_evidence_type_valid"),
        ),
        sa.CheckConstraint(
            "char_length(excerpt) BETWEEN 1 AND 8000", name=op.f("ck_evidence_excerpt_length")
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_evidence_content_sha256_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'", name=op.f("ck_evidence_metadata_is_object")
        ),
        sa.CheckConstraint(
            "privacy_scope IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name=op.f("ck_evidence_privacy_scope_valid"),
        ),
    )

    op.create_table(
        "fact_version_evidence",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("linked_by_user_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint(
            "fact_version_id", "evidence_id", name=op.f("pk_fact_version_evidence")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_fact_version_evidence_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            name=op.f("fk_fact_version_evidence_organization_id_fact_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "evidence_id"],
            ["evidence.organization_id", "evidence.id"],
            name=op.f("fk_fact_version_evidence_organization_id_evidence"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["linked_by_user_id"],
            ["users.id"],
            name=op.f("fk_fact_version_evidence_linked_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "relation IN ('SUPPORTS', 'CONTRADICTS')",
            name=op.f("ck_fact_version_evidence_relation_valid"),
        ),
    )

    op.create_index(
        op.f("ix_fact_version_evidence_evidence_id"), "fact_version_evidence", ["evidence_id"]
    )

    # Reusable guard: provenance tables can only be inserted into, never changed.
    op.execute(
        """
        CREATE FUNCTION contextledger_forbid_modification() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: rows cannot be updated or deleted', TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
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
    op.execute("DROP FUNCTION IF EXISTS contextledger_forbid_modification()")
    op.drop_index(op.f("ix_fact_version_evidence_evidence_id"), table_name="fact_version_evidence")
    op.drop_table("fact_version_evidence")
    op.drop_table("evidence")
    op.drop_constraint(op.f("uq_fact_versions_organization_id_id"), "fact_versions", type_="unique")
