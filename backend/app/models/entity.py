from sqlalchemy import CheckConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantOwnedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Entity(UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, Base):
    """Something facts are about, identified by (entity_type, external_id) within a tenant.

    Example: entity_type="customer", external_id="customer-991".
    """

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("organization_id", "entity_type", "external_id"),
        # Target of composite (organization_id, entity_id) foreign keys, which
        # make it impossible for a fact to point at another tenant's entity.
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            r"entity_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'", name="entity_type_format"
        ),
        CheckConstraint("char_length(external_id) BETWEEN 1 AND 256", name="external_id_length"),
    )

    entity_type: Mapped[str] = mapped_column(String(128))
    external_id: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str | None] = mapped_column(String(200))
