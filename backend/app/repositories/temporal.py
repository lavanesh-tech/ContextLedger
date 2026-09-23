"""SQL implementation of temporal resolution (tenant-bound).

Implements the semantics defined in ``app/temporal/reference.py`` directly in
PostgreSQL, so later phases (hybrid retrieval, decision receipts) can combine
"valid at T as known at K" with other filters in a single query.

Condition "version is valid at T as known at K":

    recorded_at <= K                                      -- known at K
    AND valid_from <= T                                   -- started by T
    AND ( valid_until IS NULL                             -- open-ended
          OR valid_until_recorded_at > K                  -- end not yet known at K
          OR T < valid_until )                            -- not yet ended at T

With K = "latest knowledge", the first and fourth lines are dropped.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import Entity
from app.models.fact import Fact, FactVersion


def valid_at_condition(valid_at: datetime, known_at: datetime | None) -> ColumnElement[bool]:
    """SQL condition: the fact version is valid at ``valid_at`` as known at ``known_at``."""
    if known_at is None:
        return and_(
            FactVersion.valid_from <= valid_at,
            or_(FactVersion.valid_until.is_(None), FactVersion.valid_until > valid_at),
        )
    return and_(
        FactVersion.recorded_at <= known_at,
        FactVersion.valid_from <= valid_at,
        or_(
            FactVersion.valid_until.is_(None),
            FactVersion.valid_until_recorded_at > known_at,
            FactVersion.valid_until > valid_at,
        ),
    )


class TemporalRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self.organization_id = organization_id

    async def get_entity(self, *, entity_type: str, external_id: str) -> Entity | None:
        result = await self._session.scalars(
            select(Entity).where(
                Entity.organization_id == self.organization_id,
                Entity.entity_type == entity_type,
                Entity.external_id == external_id,
            )
        )
        return result.one_or_none()

    async def facts_valid_at(
        self, *, entity_id: UUID, valid_at: datetime, known_at: datetime | None
    ) -> Sequence[tuple[FactVersion, str]]:
        """(version, property) for every fact of the entity with a value at ``valid_at``.

        DISTINCT ON is a safety net: the exclusion constraint already guarantees
        at most one match per fact.
        """
        statement = (
            select(FactVersion, Fact.property)
            .join(
                Fact,
                and_(
                    Fact.organization_id == FactVersion.organization_id,
                    Fact.id == FactVersion.fact_id,
                ),
            )
            .where(
                FactVersion.organization_id == self.organization_id,
                Fact.entity_id == entity_id,
                valid_at_condition(valid_at, known_at),
            )
            .distinct(FactVersion.fact_id)
            .order_by(FactVersion.fact_id, FactVersion.version.desc())
        )
        result = await self._session.execute(statement)
        return [(version, prop) for version, prop in result.tuples()]

    async def entity_versions(
        self, *, entity_id: UUID, known_at: datetime | None
    ) -> Sequence[tuple[FactVersion, str]]:
        """Every version of every fact of the entity recorded by ``known_at``."""
        statement = (
            select(FactVersion, Fact.property)
            .join(
                Fact,
                and_(
                    Fact.organization_id == FactVersion.organization_id,
                    Fact.id == FactVersion.fact_id,
                ),
            )
            .where(
                FactVersion.organization_id == self.organization_id,
                Fact.entity_id == entity_id,
            )
            .order_by(Fact.property, FactVersion.version)
        )
        if known_at is not None:
            statement = statement.where(FactVersion.recorded_at <= known_at)
        result = await self._session.execute(statement)
        return [(version, prop) for version, prop in result.tuples()]
