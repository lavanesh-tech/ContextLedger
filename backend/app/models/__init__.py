"""SQLAlchemy ORM models (PostgreSQL is the system of record).

Every model module must be imported here so that ``Base.metadata`` is complete
when Alembic autogenerates or checks migrations. First models arrive in Phase 3.
"""

from app.db.base import Base

__all__ = ["Base"]
