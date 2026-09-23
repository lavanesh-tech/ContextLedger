from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A human identity. Global (not tenant-owned): access comes from memberships.

    No password column: authentication is delegated to OAuth2/JWT in Phase 13.
    Users are deactivated, not deleted, so audit history keeps its references.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("position('@' in email) > 1", name="email_format"),
    )

    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
