import uuid

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import StringEnum
from app.domain.roles import MembershipRole
from app.models.mixins import TenantOwnedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class OrganizationMembership(UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, Base):
    """Grants one user one role inside one organization."""

    __tablename__ = "organization_memberships"
    __table_args__ = (
        # Leading organization_id also serves "list members of org X" queries.
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint("role IN ('ADMIN', 'ENGINEER', 'VIEWER')", name="role_valid"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,  # "which organizations does this user belong to?"
    )
    role: Mapped[MembershipRole] = mapped_column(StringEnum(MembershipRole, length=16))
