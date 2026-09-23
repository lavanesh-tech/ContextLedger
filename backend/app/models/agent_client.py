"""OAuth2 clients for AI agents (client-credentials grant).

An agent acts through its own **service user**, a normal ``users`` row that is
a member of exactly one organization with a non-ADMIN role. Every existing
membership and RBAC check therefore applies to agents unchanged. The token
additionally narrows permissions to ``allowed_scopes`` and privacy to
``max_privacy_scope``. Only a scrypt hash of the secret is stored.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import StringEnum
from app.domain.facts import PrivacyScope
from app.models.mixins import TenantOwnedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AgentClient(UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, Base):
    __tablename__ = "agent_clients"
    __table_args__ = (
        UniqueConstraint("client_id"),
        UniqueConstraint("service_user_id"),
        UniqueConstraint("organization_id", "name"),
        CheckConstraint(
            "privacy_ceiling IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name="privacy_ceiling_valid",
        ),
    )

    name: Mapped[str] = mapped_column(String(200))
    client_id: Mapped[str] = mapped_column(String(64))
    secret_hash: Mapped[str] = mapped_column(String(200))
    service_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    allowed_scopes: Mapped[list[str]] = mapped_column(JSONB)
    privacy_ceiling: Mapped[PrivacyScope] = mapped_column(StringEnum(PrivacyScope, length=16))
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    revoked_at: Mapped[datetime | None]
