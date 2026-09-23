"""OAuth2 agent clients with service users.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_clients",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("secret_hash", sa.String(length=200), nullable=False),
        sa.Column("service_user_id", sa.Uuid(), nullable=False),
        sa.Column("allowed_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("privacy_ceiling", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_clients")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_clients_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_user_id"],
            ["users.id"],
            name=op.f("fk_agent_clients_service_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_agent_clients_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("client_id", name=op.f("uq_agent_clients_client_id")),
        sa.UniqueConstraint("service_user_id", name=op.f("uq_agent_clients_service_user_id")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_agent_clients_organization_id_name")
        ),
        sa.CheckConstraint(
            "privacy_ceiling IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name=op.f("ck_agent_clients_privacy_ceiling_valid"),
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_clients")
