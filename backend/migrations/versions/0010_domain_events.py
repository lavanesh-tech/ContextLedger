"""Domain events: transactional outbox for Kafka, consumer dedup, activity read model.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table -> (event type, operations, aggregate-id column, payload columns).
# Payloads carry identifiers and metadata only, never fact values, excerpts or
# decision outcomes: Kafka consumers do not enforce privacy scopes.
EMITTING_TABLES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "fact_versions": (
        "fact.version_recorded",
        "INSERT",
        "id",
        (
            "id",
            "fact_id",
            "version",
            "source_id",
            "supersedes_id",
            "valid_from",
            "valid_until",
            "authority",
            "privacy_scope",
            "recorded_at",
        ),
    ),
    "fact_embeddings": (
        "fact.embedding_stored",
        "INSERT OR UPDATE",
        "fact_version_id",
        ("fact_version_id", "model", "created_at"),
    ),
    "evidence": (
        "evidence.captured",
        "INSERT",
        "id",
        (
            "id",
            "source_id",
            "evidence_type",
            "content_sha256",
            "privacy_scope",
            "captured_at",
            "recorded_at",
        ),
    ),
    "fact_version_evidence": (
        "evidence.linked",
        "INSERT",
        "fact_version_id",
        ("fact_version_id", "evidence_id", "relation", "linked_at"),
    ),
    "context_snapshots": (
        "context.captured",
        "INSERT",
        "id",
        ("id", "captured_by_user_id", "valid_at", "known_at", "embedding_model", "created_at"),
    ),
    "decisions": (
        "decision.recorded",
        "INSERT",
        "id",
        (
            "id",
            "snapshot_id",
            "decided_by_user_id",
            "agent",
            "action",
            "decided_at",
            "receipt_sha256",
        ),
    ),
}

EMIT_FUNCTION = """
CREATE FUNCTION contextledger_emit_event() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_json jsonb := to_jsonb(NEW);
    payload jsonb := '{}'::jsonb;
    i integer;
BEGIN
    -- TG_ARGV: [0] event type, [1] aggregate-id column, [2..] payload columns.
    FOR i IN 2 .. TG_NARGS - 1 LOOP
        payload := payload || jsonb_build_object(TG_ARGV[i], row_json -> TG_ARGV[i]);
    END LOOP;
    INSERT INTO event_outbox (organization_id, event_type, aggregate_id, payload)
    VALUES (NEW.organization_id, TG_ARGV[0], (row_json ->> TG_ARGV[1])::uuid, payload);
    RETURN NULL;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "event_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")),
        sa.UniqueConstraint("event_id", name=op.f("uq_event_outbox_event_id")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_event_outbox_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_event_outbox_organization_id_id", "event_outbox", ["organization_id", "id"])

    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.String(length=100), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_processed_events")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_processed_events_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_processed_events_organization_id_processed_at",
        "processed_events",
        ["organization_id", "processed_at"],
    )

    op.create_table(
        "organization_activity_daily",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("facts_recorded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("evidence_captured", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("decisions_recorded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "day", name=op.f("pk_organization_activity_daily")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_activity_daily_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
    )

    op.execute(EMIT_FUNCTION)
    for table, (event_type, operations, aggregate, columns) in EMITTING_TABLES.items():
        args = ", ".join(f"'{value}'" for value in (event_type, aggregate, *columns))
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_emit_event
            AFTER {operations} ON {table}
            FOR EACH ROW EXECUTE FUNCTION contextledger_emit_event({args})
            """
        )


def downgrade() -> None:
    for table in EMITTING_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_emit_event ON {table}")
    op.execute("DROP FUNCTION IF EXISTS contextledger_emit_event()")
    op.drop_table("organization_activity_daily")
    op.drop_index("ix_processed_events_organization_id_processed_at", table_name="processed_events")
    op.drop_table("processed_events")
    op.drop_index("ix_event_outbox_organization_id_id", table_name="event_outbox")
    op.drop_table("event_outbox")
