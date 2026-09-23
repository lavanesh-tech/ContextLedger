"""Organizations, users and organization memberships (tenant foundation).

Migrations are frozen snapshots: they do not import application code, so
later changes to models or enums cannot silently change this revision.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column[object]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "organizations",
        _id(),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
        sa.CheckConstraint(
            r"slug ~ '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'",
            name=op.f("ck_organizations_slug_format"),
        ),
    )

    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.CheckConstraint("position('@' in email) > 1", name=op.f("ck_users_email_format")),
    )

    op.create_table(
        "organization_memberships",
        _id(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_memberships")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_memberships_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_organization_memberships_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            name=op.f("uq_organization_memberships_organization_id_user_id"),
        ),
        sa.CheckConstraint(
            "role IN ('ADMIN', 'ENGINEER', 'VIEWER')",
            name=op.f("ck_organization_memberships_role_valid"),
        ),
    )
    op.create_index(
        op.f("ix_organization_memberships_user_id"),
        "organization_memberships",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_organization_memberships_user_id"), table_name="organization_memberships"
    )
    op.drop_table("organization_memberships")
    op.drop_table("users")
    op.drop_table("organizations")
