from sqlalchemy import CheckConstraint, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import StringEnum
from app.domain.facts import SourceType
from app.models.mixins import TenantOwnedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class FactSource(UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, Base):
    """Where fact versions come from (billing DB, CRM API, a document, a person, an agent)."""

    __tablename__ = "fact_sources"
    __table_args__ = (
        UniqueConstraint("organization_id", "name"),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            "source_type IN ('SYSTEM_OF_RECORD', 'API', 'DOCUMENT', 'HUMAN', 'AGENT')",
            name="source_type_valid",
        ),
        CheckConstraint("default_authority BETWEEN 0 AND 100", name="default_authority_range"),
    )

    name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[SourceType] = mapped_column(StringEnum(SourceType, length=32))
    uri: Mapped[str | None] = mapped_column(String(2048))
    # How much this source is trusted (0-100); copied onto each version it asserts
    # unless the caller overrides it. Used for ranking in Phase 8.
    default_authority: Mapped[int] = mapped_column(SmallInteger)
