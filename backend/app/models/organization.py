from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant. Every tenant-owned row in the system references one."""

    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(r"slug ~ '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'", name="slug_format"),
    )

    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(200))
