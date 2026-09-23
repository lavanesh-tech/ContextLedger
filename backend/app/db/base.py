"""Declarative base shared by every ORM model."""

from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names. Without them PostgreSQL invents names, and
# Alembic cannot reliably drop or alter those constraints in later migrations.
NAMING_CONVENTION: Final = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Every ``Mapped[datetime]`` column is TIMESTAMP WITH TIME ZONE. Naive
    # timestamps would make "what was valid at time T?" ambiguous.
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012 (read by SQLAlchemy)
