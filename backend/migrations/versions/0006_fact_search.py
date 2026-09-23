"""Full-text search documents for fact versions, maintained by a trigger.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of the searchable text for a fact version. Migrations never import
# application code, so later changes to the app cannot change what this revision does.
SEARCH_TEXT_FUNCTION = """
CREATE FUNCTION contextledger_fact_search_text(
    entity_type text, external_id text, property text, value jsonb
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT translate(entity_type, '_.', '  ') || ' ' || external_id || ' '
        || translate(property, '_.', '  ') || ' '
        || CASE jsonb_typeof(value) WHEN 'string' THEN value #>> '{}' ELSE value::text END
$$
"""

INDEX_TRIGGER_FUNCTION = """
CREATE FUNCTION contextledger_index_fact_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO fact_search_documents (organization_id, fact_version_id, search_vector)
    SELECT NEW.organization_id,
           NEW.id,
           to_tsvector(
               'english',
               contextledger_fact_search_text(e.entity_type, e.external_id, f.property, NEW.value)
           )
    FROM facts f
    JOIN entities e ON e.organization_id = f.organization_id AND e.id = f.entity_id
    WHERE f.organization_id = NEW.organization_id AND f.id = NEW.fact_id;
    RETURN NULL;
END;
$$
"""

BACKFILL = """
INSERT INTO fact_search_documents (organization_id, fact_version_id, search_vector)
SELECT v.organization_id,
       v.id,
       to_tsvector(
           'english',
           contextledger_fact_search_text(e.entity_type, e.external_id, f.property, v.value)
       )
FROM fact_versions v
JOIN facts f ON f.organization_id = v.organization_id AND f.id = v.fact_id
JOIN entities e ON e.organization_id = f.organization_id AND e.id = f.entity_id
"""


def upgrade() -> None:
    op.create_table(
        "fact_search_documents",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version_id", sa.Uuid(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("fact_version_id", name=op.f("pk_fact_search_documents")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_fact_search_documents_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            name=op.f("fk_fact_search_documents_organization_id_fact_versions"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_fact_search_documents_search_vector",
        "fact_search_documents",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.execute(SEARCH_TEXT_FUNCTION)
    op.execute(INDEX_TRIGGER_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_fact_versions_index_search
        AFTER INSERT ON fact_versions
        FOR EACH ROW EXECUTE FUNCTION contextledger_index_fact_version()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fact_search_documents_immutable
        BEFORE UPDATE OR DELETE ON fact_search_documents
        FOR EACH ROW EXECUTE FUNCTION contextledger_forbid_modification()
        """
    )
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_fact_versions_index_search ON fact_versions")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_fact_search_documents_immutable ON fact_search_documents"
    )
    op.execute("DROP FUNCTION IF EXISTS contextledger_index_fact_version()")
    op.execute("DROP FUNCTION IF EXISTS contextledger_fact_search_text(text, text, text, jsonb)")
    op.drop_index("ix_fact_search_documents_search_vector", table_name="fact_search_documents")
    op.drop_table("fact_search_documents")
