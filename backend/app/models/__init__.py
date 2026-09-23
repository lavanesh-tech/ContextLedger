"""SQLAlchemy ORM models (PostgreSQL is the system of record).

Every model module must be imported here so that ``Base.metadata`` is complete
when Alembic autogenerates or checks migrations.
"""

from app.db.base import Base
from app.models.entity import Entity
from app.models.fact import Fact, FactVersion
from app.models.membership import OrganizationMembership
from app.models.organization import Organization
from app.models.source import FactSource
from app.models.user import User

__all__ = [
    "Base",
    "Entity",
    "Fact",
    "FactSource",
    "FactVersion",
    "Organization",
    "OrganizationMembership",
    "User",
]
