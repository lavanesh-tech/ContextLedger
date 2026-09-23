"""Temporal resolution service: deterministic answers to time-based questions.

* ``facts_at``        current facts, or facts valid at T, optionally as known at K
* ``history``         all versions of one fact (optionally as known at K)
* ``changes_between`` what changed for an entity between T1 and T2
* ``lineage``         which version a version superseded, and which superseded it
* ``entity_timeline`` every version of every fact of an entity, in valid-time order

Every method checks ``facts:read`` against the actor's current membership, and
only sees the caller's organization. No LLM is involved in any of these answers.
"""

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import NotFoundError, ValidationFailedError
from app.domain.facts import require_aware
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.domain.validation import normalize_external_id, normalize_identifier
from app.models.entity import Entity
from app.models.fact import FactVersion
from app.repositories.facts import FactRepository
from app.repositories.temporal import TemporalRepository
from app.services.authorization import require_permission
from app.temporal import reference
from app.temporal.model import (
    FactChange,
    Lineage,
    ResolvedFact,
    TimelineEntry,
    VersionSnapshot,
)


def to_snapshot(version: FactVersion) -> VersionSnapshot:
    return VersionSnapshot(
        id=version.id,
        fact_id=version.fact_id,
        version=version.version,
        value=version.value,
        source_id=version.source_id,
        valid_from=version.valid_from,
        valid_until=version.valid_until,
        observed_at=version.observed_at,
        recorded_at=version.recorded_at,
        valid_until_recorded_at=version.valid_until_recorded_at,
        supersedes_id=version.supersedes_id,
        authority=version.authority,
        confidence=version.confidence,
        privacy_scope=version.privacy_scope,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TemporalService:
    def __init__(self, session: AsyncSession, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def facts_at(
        self,
        ctx: TenantContext,
        *,
        entity_type: str,
        external_id: str,
        valid_at: datetime | None = None,
        known_at: datetime | None = None,
    ) -> list[ResolvedFact]:
        """Facts of one entity valid at ``valid_at`` (default: now), as known at ``known_at``.

        ``known_at=None`` uses everything recorded so far. To reconstruct what an
        agent knew when it decided at time D, pass ``valid_at=D, known_at=D``.
        """
        instant = require_aware(valid_at or self._clock(), field="valid_at")
        if known_at is not None:
            require_aware(known_at, field="known_at")

        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            repository = TemporalRepository(self._session, ctx.organization_id)
            entity = await self._require_entity(repository, entity_type, external_id)
            rows = await repository.facts_valid_at(
                entity_id=entity.id, valid_at=instant, known_at=known_at
            )
            resolved = [
                ResolvedFact(
                    fact_id=version.fact_id,
                    entity_id=entity.id,
                    entity_type=entity.entity_type,
                    external_id=entity.external_id,
                    property=prop,
                    version=reference.as_known(to_snapshot(version), known_at),
                )
                for version, prop in rows
            ]
        return sorted(resolved, key=lambda fact: fact.property)

    async def history(
        self, ctx: TenantContext, fact_id: UUID, *, known_at: datetime | None = None
    ) -> list[VersionSnapshot]:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            repository = FactRepository(self._session, ctx.organization_id)
            if await repository.get_fact(fact_id) is None:
                raise NotFoundError("fact not found")
            versions = [to_snapshot(v) for v in await repository.list_versions(fact_id)]
        return reference.history_as_known(versions, known_at)

    async def changes_between(
        self,
        ctx: TenantContext,
        *,
        entity_type: str,
        external_id: str,
        start: datetime,
        end: datetime,
        known_at: datetime | None = None,
    ) -> list[FactChange]:
        """Per fact: the value at ``start``, the value at ``end``, and every version in between."""
        require_aware(start, field="start")
        require_aware(end, field="end")
        if end <= start:
            raise ValidationFailedError("end must be later than start")
        if known_at is not None:
            require_aware(known_at, field="known_at")

        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            repository = TemporalRepository(self._session, ctx.organization_id)
            entity = await self._require_entity(repository, entity_type, external_id)
            rows = await repository.entity_versions(entity_id=entity.id, known_at=known_at)

        by_fact: dict[UUID, list[VersionSnapshot]] = defaultdict(list)
        properties: dict[UUID, str] = {}
        for version, prop in rows:
            by_fact[version.fact_id].append(to_snapshot(version))
            properties[version.fact_id] = prop

        changes: list[FactChange] = []
        for fact_id, versions in by_fact.items():
            change = reference.change_between(versions, start, end, known_at)
            if change is not None:
                before, after, transitions = change
                changes.append(
                    FactChange(
                        fact_id=fact_id,
                        property=properties[fact_id],
                        before=before,
                        after=after,
                        transitions=transitions,
                    )
                )
        return sorted(changes, key=lambda change: change.property)

    async def lineage(self, ctx: TenantContext, version_id: UUID) -> Lineage:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            repository = FactRepository(self._session, ctx.organization_id)
            target = await repository.get_version(version_id)
            if target is None:
                raise NotFoundError("fact version not found")
            chain = [to_snapshot(v) for v in await repository.list_versions(target.fact_id)]

        current = next(v for v in chain if v.id == version_id)
        return Lineage(
            version=current,
            ancestors=tuple(v for v in chain if v.version < current.version),
            descendants=tuple(v for v in chain if v.version > current.version),
        )

    async def entity_timeline(
        self,
        ctx: TenantContext,
        *,
        entity_type: str,
        external_id: str,
        known_at: datetime | None = None,
    ) -> list[TimelineEntry]:
        if known_at is not None:
            require_aware(known_at, field="known_at")
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            repository = TemporalRepository(self._session, ctx.organization_id)
            entity = await self._require_entity(repository, entity_type, external_id)
            rows = await repository.entity_versions(entity_id=entity.id, known_at=known_at)

        entries = [
            TimelineEntry(property=prop, version=reference.as_known(to_snapshot(v), known_at))
            for v, prop in rows
        ]
        return sorted(entries, key=lambda e: (e.version.valid_from, e.property, e.version.version))

    @staticmethod
    async def _require_entity(
        repository: TemporalRepository, entity_type: str, external_id: str
    ) -> Entity:
        entity = await repository.get_entity(
            entity_type=normalize_identifier(entity_type, field="entity_type"),
            external_id=normalize_external_id(external_id),
        )
        if entity is None:
            raise NotFoundError("entity not found")
        return entity
