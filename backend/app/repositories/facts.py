"""Tenant-bound data access for entities, fact sources, facts and fact versions.

Like every tenant-owned repository, it is constructed for one organization
and filters every query by it. Never commits.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.facts import PrivacyScope, SourceType
from app.models.entity import Entity
from app.models.fact import Fact, FactVersion
from app.models.source import FactSource


class FactRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self.organization_id = organization_id

    # --- sources ------------------------------------------------------------------

    def new_source(
        self, *, name: str, source_type: SourceType, uri: str | None, default_authority: int
    ) -> FactSource:
        source = FactSource(
            organization_id=self.organization_id,
            name=name,
            source_type=source_type,
            uri=uri,
            default_authority=default_authority,
        )
        self._session.add(source)
        return source

    async def get_source(self, source_id: UUID) -> FactSource | None:
        result = await self._session.scalars(
            select(FactSource).where(
                FactSource.organization_id == self.organization_id, FactSource.id == source_id
            )
        )
        return result.one_or_none()

    async def get_source_by_name(self, name: str) -> FactSource | None:
        result = await self._session.scalars(
            select(FactSource).where(
                FactSource.organization_id == self.organization_id, FactSource.name == name
            )
        )
        return result.one_or_none()

    # --- entities and facts ---------------------------------------------------------

    async def get_or_create_entity(self, *, entity_type: str, external_id: str) -> Entity:
        """Race-safe upsert: concurrent first writes resolve to one row."""
        await self._session.execute(
            insert(Entity)
            .values(
                organization_id=self.organization_id,
                entity_type=entity_type,
                external_id=external_id,
            )
            .on_conflict_do_nothing(
                index_elements=["organization_id", "entity_type", "external_id"]
            )
        )
        result = await self._session.scalars(
            select(Entity).where(
                Entity.organization_id == self.organization_id,
                Entity.entity_type == entity_type,
                Entity.external_id == external_id,
            )
        )
        return result.one()

    async def get_or_create_fact(self, *, entity_id: UUID, property_name: str) -> Fact:
        await self._session.execute(
            insert(Fact)
            .values(
                organization_id=self.organization_id, entity_id=entity_id, property=property_name
            )
            .on_conflict_do_nothing(index_elements=["organization_id", "entity_id", "property"])
        )
        result = await self._session.scalars(
            select(Fact).where(
                Fact.organization_id == self.organization_id,
                Fact.entity_id == entity_id,
                Fact.property == property_name,
            )
        )
        return result.one()

    async def get_fact(self, fact_id: UUID) -> Fact | None:
        result = await self._session.scalars(
            select(Fact).where(Fact.organization_id == self.organization_id, Fact.id == fact_id)
        )
        return result.one_or_none()

    async def lock_fact(self, fact_id: UUID) -> None:
        """Serialise version writes per fact (SELECT ... FOR UPDATE on the fact row)."""
        await self._session.execute(
            select(Fact.id)
            .where(Fact.organization_id == self.organization_id, Fact.id == fact_id)
            .with_for_update()
        )

    # --- versions ------------------------------------------------------------------

    async def latest_version(self, fact_id: UUID) -> FactVersion | None:
        result = await self._session.scalars(
            select(FactVersion)
            .where(
                FactVersion.organization_id == self.organization_id,
                FactVersion.fact_id == fact_id,
            )
            .order_by(FactVersion.version.desc())
            .limit(1)
        )
        return result.one_or_none()

    async def close_version(self, version: FactVersion, valid_until: datetime) -> None:
        """Set the end of an open version. The DB trigger allows this exactly once."""
        await self._session.execute(
            update(FactVersion)
            .where(
                FactVersion.organization_id == self.organization_id,
                FactVersion.id == version.id,
                FactVersion.valid_until.is_(None),
            )
            .values(valid_until=valid_until, valid_until_recorded_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await self._session.refresh(version)

    def new_version(
        self,
        *,
        fact_id: UUID,
        version: int,
        value: Any,
        source_id: UUID,
        valid_from: datetime,
        valid_until: datetime | None,
        observed_at: datetime,
        supersedes_id: UUID | None,
        authority: int,
        confidence: Decimal,
        privacy_scope: PrivacyScope,
    ) -> FactVersion:
        fact_version = FactVersion(
            organization_id=self.organization_id,
            fact_id=fact_id,
            version=version,
            value=value,
            source_id=source_id,
            valid_from=valid_from,
            valid_until=valid_until,
            # A version created with a fixed end "learns" that end at insert time.
            valid_until_recorded_at=None if valid_until is None else func.now(),
            observed_at=observed_at,
            supersedes_id=supersedes_id,
            authority=authority,
            confidence=confidence,
            privacy_scope=privacy_scope,
        )
        self._session.add(fact_version)
        return fact_version

    async def get_version(self, version_id: UUID) -> FactVersion | None:
        result = await self._session.scalars(
            select(FactVersion).where(
                FactVersion.organization_id == self.organization_id,
                FactVersion.id == version_id,
            )
        )
        return result.one_or_none()

    async def list_versions(self, fact_id: UUID) -> Sequence[FactVersion]:
        result = await self._session.scalars(
            select(FactVersion)
            .where(
                FactVersion.organization_id == self.organization_id,
                FactVersion.fact_id == fact_id,
            )
            .order_by(FactVersion.version)
        )
        return result.all()
