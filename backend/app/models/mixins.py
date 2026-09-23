"""Reusable column sets for ORM models."""

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import ForeignKey, func, text
from sqlalchemy.orm import Mapped, mapped_column

# Tables that are intentionally NOT owned by a tenant. Every other table must
# use TenantOwnedMixin; tests/unit/test_tenant_ownership.py enforces this so a
# future table cannot silently skip tenant isolation.
GLOBAL_TABLES: Final = frozenset({"organizations", "users"})


class UUIDPrimaryKeyMixin:
    # Generated in Python so the id is known before INSERT; the server default
    # covers rows inserted by SQL outside the ORM.
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    # Fetch server-generated values (now()) with RETURNING on INSERT *and*
    # UPDATE. Otherwise they would be lazy-loaded on first access, which is
    # implicit I/O and fails in async code.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"),
        onupdate=func.now(),
    )


class TenantOwnedMixin:
    """Marks a row as belonging to exactly one organization (tenant).

    No index is declared here: each table adds a composite index/unique
    constraint that *starts* with organization_id, which serves tenant-filtered
    queries better than a single-column index.
    """

    # RESTRICT: an organization that still owns data cannot be deleted by accident.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
    )
