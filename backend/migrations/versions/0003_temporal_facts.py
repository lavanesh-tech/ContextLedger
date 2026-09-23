"""Temporal fact domain: entities, fact sources, facts, fact versions.

Adds database-level guarantees that application code alone cannot give:
* composite foreign keys that keep every reference inside one tenant;
* an EXCLUDE constraint so versions of one fact never overlap in valid time;
* a trigger that makes fact_versions append-only.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTIFIER_REGEX = r"'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'"


def _id() -> sa.Column[object]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _org() -> sa.Column[object]:
    return sa.Column("organization_id", sa.Uuid(), nullable=False)


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="RESTRICT",
    )


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            name,
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
        for name in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    # btree_gist lets a GiST index combine "=" on a uuid with "&&" on a range.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "entities",
        _id(),
        _org(),
        sa.Column("entity_type", sa.String(length=128), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
        _org_fk("entities"),
        sa.UniqueConstraint(
            "organization_id",
            "entity_type",
            "external_id",
            name=op.f("uq_entities_organization_id_entity_type_external_id"),
        ),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_entities_organization_id_id")),
        sa.CheckConstraint(
            f"entity_type ~ {IDENTIFIER_REGEX}", name=op.f("ck_entities_entity_type_format")
        ),
        sa.CheckConstraint(
            "char_length(external_id) BETWEEN 1 AND 256",
            name=op.f("ck_entities_external_id_length"),
        ),
    )

    op.create_table(
        "fact_sources",
        _id(),
        _org(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("uri", sa.String(length=2048), nullable=True),
        sa.Column("default_authority", sa.SmallInteger(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fact_sources")),
        _org_fk("fact_sources"),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_fact_sources_organization_id_name")
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_fact_sources_organization_id_id")
        ),
        sa.CheckConstraint(
            "source_type IN ('SYSTEM_OF_RECORD', 'API', 'DOCUMENT', 'HUMAN', 'AGENT')",
            name=op.f("ck_fact_sources_source_type_valid"),
        ),
        sa.CheckConstraint(
            "default_authority BETWEEN 0 AND 100",
            name=op.f("ck_fact_sources_default_authority_range"),
        ),
    )

    op.create_table(
        "facts",
        _id(),
        _org(),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("property", sa.String(length=128), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_facts")),
        _org_fk("facts"),
        sa.ForeignKeyConstraint(
            ["organization_id", "entity_id"],
            ["entities.organization_id", "entities.id"],
            name=op.f("fk_facts_organization_id_entities"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "entity_id",
            "property",
            name=op.f("uq_facts_organization_id_entity_id_property"),
        ),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_facts_organization_id_id")),
        sa.CheckConstraint(f"property ~ {IDENTIFIER_REGEX}", name=op.f("ck_facts_property_format")),
    )

    op.create_table(
        "fact_versions",
        _id(),
        _org(),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("authority", sa.SmallInteger(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("privacy_scope", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fact_versions")),
        _org_fk("fact_versions"),
        sa.ForeignKeyConstraint(
            ["organization_id", "fact_id"],
            ["facts.organization_id", "facts.id"],
            name=op.f("fk_fact_versions_organization_id_facts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_id"],
            ["fact_sources.organization_id", "fact_sources.id"],
            name=op.f("fk_fact_versions_organization_id_fact_sources"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("fact_id", "version", name=op.f("uq_fact_versions_fact_id_version")),
        sa.UniqueConstraint("fact_id", "id", name=op.f("uq_fact_versions_fact_id_id")),
        sa.UniqueConstraint("supersedes_id", name=op.f("uq_fact_versions_supersedes_id")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_fact_versions_version_positive")),
        sa.CheckConstraint(
            "(version = 1) = (supersedes_id IS NULL)",
            name=op.f("ck_fact_versions_lineage_consistent"),
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name=op.f("ck_fact_versions_valid_range_ordered"),
        ),
        sa.CheckConstraint(
            "(valid_until IS NULL) = (valid_until_recorded_at IS NULL)",
            name=op.f("ck_fact_versions_valid_until_recorded_together"),
        ),
        sa.CheckConstraint(
            "authority BETWEEN 0 AND 100", name=op.f("ck_fact_versions_authority_range")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_fact_versions_confidence_range")
        ),
        sa.CheckConstraint(
            "privacy_scope IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name=op.f("ck_fact_versions_privacy_scope_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(value) <> 'null'", name=op.f("ck_fact_versions_value_not_null")
        ),
    )
    # Self-reference is added after the table exists (it targets uq_fact_versions_fact_id_id).
    op.create_foreign_key(
        op.f("fk_fact_versions_fact_id_fact_versions"),
        "fact_versions",
        "fact_versions",
        ["fact_id", "supersedes_id"],
        ["fact_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_fact_versions_organization_id_fact_id_valid_from"),
        "fact_versions",
        ["organization_id", "fact_id", "valid_from"],
    )

    # No two versions of one fact may be valid at the same instant.
    # '[)' = half-open interval; a NULL upper bound means "open-ended".
    op.execute(
        """
        ALTER TABLE fact_versions
        ADD CONSTRAINT ex_fact_versions_no_overlapping_validity
        EXCLUDE USING gist (
            fact_id WITH =,
            tstzrange(valid_from, valid_until, '[)') WITH &&
        )
        """
    )

    # Append-only history: no deletes, no edits. The single allowed update is
    # closing an open version once (setting valid_until + valid_until_recorded_at).
    op.execute(
        """
        CREATE FUNCTION fact_versions_enforce_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'fact_versions is append-only: rows cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF (NEW.id, NEW.organization_id, NEW.fact_id, NEW.version, NEW.value,
                NEW.source_id, NEW.valid_from, NEW.observed_at, NEW.recorded_at,
                NEW.supersedes_id, NEW.authority, NEW.confidence, NEW.privacy_scope)
               IS DISTINCT FROM
               (OLD.id, OLD.organization_id, OLD.fact_id, OLD.version, OLD.value,
                OLD.source_id, OLD.valid_from, OLD.observed_at, OLD.recorded_at,
                OLD.supersedes_id, OLD.authority, OLD.confidence, OLD.privacy_scope) THEN
                RAISE EXCEPTION 'fact_versions is append-only: record a new version instead'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.valid_until IS NOT NULL AND
               (NEW.valid_until, NEW.valid_until_recorded_at)
               IS DISTINCT FROM (OLD.valid_until, OLD.valid_until_recorded_at) THEN
                RAISE EXCEPTION 'fact_versions: valid_until can only be set once'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fact_versions_append_only
        BEFORE UPDATE OR DELETE ON fact_versions
        FOR EACH ROW EXECUTE FUNCTION fact_versions_enforce_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_fact_versions_append_only ON fact_versions")
    op.execute("DROP FUNCTION IF EXISTS fact_versions_enforce_append_only()")
    op.drop_table("fact_versions")
    op.drop_table("facts")
    op.drop_table("fact_sources")
    op.drop_table("entities")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
