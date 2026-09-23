"""Fact embeddings (pgvector) with an HNSW index, and the embedding job queue.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIMENSIONS = 1536


def _org_fks(table: str) -> list[sa.ForeignKeyConstraint]:
    return [
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f(f"fk_{table}_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            name=op.f(f"fk_{table}_organization_id_fact_versions"),
            ondelete="RESTRICT",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "fact_embeddings",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("text_template", sa.String(length=32), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("fact_version_id", "model", name=op.f("pk_fact_embeddings")),
        *_org_fks("fact_embeddings"),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_fact_embeddings_content_sha256_format"),
        ),
        sa.CheckConstraint(
            f"dimensions = {DIMENSIONS}", name=op.f("ck_fact_embeddings_dimensions_supported")
        ),
    )
    # Added with raw SQL: migrations stay independent of application types.
    op.execute(f"ALTER TABLE fact_embeddings ADD COLUMN embedding vector({DIMENSIONS}) NOT NULL")
    op.create_index(
        "ix_fact_embeddings_organization_id_model_content_sha256",
        "fact_embeddings",
        ["organization_id", "model", "content_sha256"],
    )
    # HNSW with cosine distance. m / ef_construction are pgvector's defaults, chosen
    # after the HNSW vs IVFFlat comparison in benchmarks/ (see docs/VECTOR_INDEXING.md).
    op.execute(
        "CREATE INDEX ann_fact_embeddings_embedding_cosine ON fact_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "embedding_jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_jobs")),
        *_org_fks("embedding_jobs"),
        sa.UniqueConstraint(
            "fact_version_id", "model", name=op.f("uq_embedding_jobs_fact_version_id_model")
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_embedding_jobs_status_valid"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_embedding_jobs_attempts_non_negative")),
        sa.CheckConstraint(
            "(status = 'RUNNING') = (lease_token IS NOT NULL)",
            name=op.f("ck_embedding_jobs_lease_only_while_running"),
        ),
    )
    op.create_index(
        "ix_embedding_jobs_model_status_available_at",
        "embedding_jobs",
        ["model", "status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_embedding_jobs_model_status_available_at", table_name="embedding_jobs")
    op.drop_table("embedding_jobs")
    op.execute("DROP INDEX IF EXISTS ann_fact_embeddings_embedding_cosine")
    op.drop_index(
        "ix_fact_embeddings_organization_id_model_content_sha256", table_name="fact_embeddings"
    )
    op.drop_table("fact_embeddings")
