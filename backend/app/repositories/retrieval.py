"""SQL side of hybrid retrieval (tenant-bound).

Both branches share one set of hard filters, applied *inside* each branch's
query (pre-filtering), so a vector or keyword match that is out of scope can
never take a slot in the candidate list:

* tenant:     ``organization_id``
* time:       ``valid_at_condition(valid_at, known_at)`` (same definition as Phase 5)
* privacy:    ``privacy_scope IN (...)``
* metadata:   entity type / ids, properties, sources, minimum authority / confidence

Branches:

* **vector**: cosine distance on ``fact_embeddings`` (HNSW), same embedding model only;
* **full text**: ``ts_rank_cd`` on ``fact_search_documents`` (GIN), OR-combined terms.

Both run in ONE statement (``UNION ALL``), so they read the same snapshot.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    Text,
    and_,
    cast,
    func,
    literal,
    literal_column,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.facts import PrivacyScope, SourceType
from app.domain.retrieval import TextHit, VectorHit
from app.models.embedding import FactEmbedding
from app.models.entity import Entity
from app.models.fact import Fact, FactVersion
from app.models.search import SEARCH_CONFIG, FactSearchDocument
from app.models.source import FactSource
from app.repositories.temporal import valid_at_condition

_REGCONFIG: ColumnElement[Any] = literal_column(f"'{SEARCH_CONFIG}'::regconfig")


def or_tsquery(query: str) -> ColumnElement[Any]:
    """``plainto_tsquery`` (stemming, stop words, safe parsing) with OR instead of AND.

    Natural-language questions rarely contain *every* word of a fact, so any
    matching term makes a candidate; ``ts_rank_cd`` ranks versions matching more
    terms higher.
    """
    plain = cast(func.plainto_tsquery(_REGCONFIG, query), Text)
    return cast(func.replace(plain, " & ", " | "), TSQUERY)


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    entity_type: str | None = None
    external_ids: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()
    source_ids: tuple[UUID, ...] = ()
    min_authority: int | None = None
    min_confidence: Decimal | None = None


@dataclass(frozen=True, slots=True)
class HydratedVersion:
    version: FactVersion
    property: str
    entity_type: str
    external_id: str
    source_name: str
    source_type: SourceType


class RetrievalRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self.organization_id = organization_id

    def _scoped(
        self,
        statement: Select[Any],
        *,
        valid_at: datetime,
        known_at: datetime | None,
        scopes: frozenset[PrivacyScope],
        filters: RetrievalFilters,
    ) -> Select[Any]:
        statement = (
            statement.join(
                Fact,
                and_(
                    Fact.organization_id == FactVersion.organization_id,
                    Fact.id == FactVersion.fact_id,
                ),
            )
            .join(
                Entity,
                and_(Entity.organization_id == Fact.organization_id, Entity.id == Fact.entity_id),
            )
            .where(
                FactVersion.organization_id == self.organization_id,
                valid_at_condition(valid_at, known_at),
                FactVersion.privacy_scope.in_(sorted(scopes)),
            )
        )
        if filters.entity_type is not None:
            statement = statement.where(Entity.entity_type == filters.entity_type)
        if filters.external_ids:
            statement = statement.where(Entity.external_id.in_(filters.external_ids))
        if filters.properties:
            statement = statement.where(Fact.property.in_(filters.properties))
        if filters.source_ids:
            statement = statement.where(FactVersion.source_id.in_(filters.source_ids))
        if filters.min_authority is not None:
            statement = statement.where(FactVersion.authority >= filters.min_authority)
        if filters.min_confidence is not None:
            statement = statement.where(FactVersion.confidence >= filters.min_confidence)
        return statement

    async def candidates(
        self,
        *,
        query: str,
        query_vector: Sequence[float] | None,
        model: str,
        valid_at: datetime,
        known_at: datetime | None,
        scopes: frozenset[PrivacyScope],
        filters: RetrievalFilters,
        pool: int,
    ) -> tuple[list[VectorHit], list[TextHit]]:
        def scoped(statement: Select[Any]) -> Select[Any]:
            return self._scoped(
                statement, valid_at=valid_at, known_at=known_at, scopes=scopes, filters=filters
            )

        tsquery = or_tsquery(query)
        rank = func.ts_rank_cd(FactSearchDocument.search_vector, tsquery)
        text_branch = (
            scoped(
                select(
                    literal("text").label("branch"),
                    FactSearchDocument.fact_version_id.label("id"),
                    rank.label("score"),
                ).join(
                    FactVersion,
                    and_(
                        FactVersion.organization_id == FactSearchDocument.organization_id,
                        FactVersion.id == FactSearchDocument.fact_version_id,
                    ),
                ),
            )
            .where(FactSearchDocument.search_vector.op("@@")(tsquery))
            .order_by(rank.desc(), FactSearchDocument.fact_version_id)
            .limit(pool)
        )
        branches = [text_branch.subquery()]

        if query_vector is not None:
            distance = FactEmbedding.embedding.cosine_distance(list(query_vector))
            vector_branch = (
                scoped(
                    select(
                        literal("vector").label("branch"),
                        FactEmbedding.fact_version_id.label("id"),
                        distance.label("score"),
                    ).join(
                        FactVersion,
                        and_(
                            FactVersion.organization_id == FactEmbedding.organization_id,
                            FactVersion.id == FactEmbedding.fact_version_id,
                        ),
                    ),
                )
                .where(FactEmbedding.model == model)
                .order_by(distance)
                .limit(pool)
            )
            branches.append(vector_branch.subquery())

        combined = union_all(*(select(b.c.branch, b.c.id, b.c.score) for b in branches))
        rows = (await self._session.execute(combined)).all()
        vector_hits = [VectorHit(r.id, float(r.score)) for r in rows if r.branch == "vector"]
        text_hits = [TextHit(r.id, float(r.score)) for r in rows if r.branch == "text"]
        return vector_hits, text_hits

    async def hydrate(self, version_ids: Sequence[UUID]) -> dict[UUID, HydratedVersion]:
        if not version_ids:
            return {}
        statement = (
            select(
                FactVersion,
                Fact.property,
                Entity.entity_type,
                Entity.external_id,
                FactSource.name,
                FactSource.source_type,
            )
            .join(
                Fact,
                and_(
                    Fact.organization_id == FactVersion.organization_id,
                    Fact.id == FactVersion.fact_id,
                ),
            )
            .join(
                Entity,
                and_(Entity.organization_id == Fact.organization_id, Entity.id == Fact.entity_id),
            )
            .join(
                FactSource,
                and_(
                    FactSource.organization_id == FactVersion.organization_id,
                    FactSource.id == FactVersion.source_id,
                ),
            )
            .where(
                FactVersion.organization_id == self.organization_id,
                FactVersion.id.in_(version_ids),
            )
        )
        result = await self._session.execute(statement)
        return {
            version.id: HydratedVersion(version, prop, entity_type, external_id, name, kind)
            for version, prop, entity_type, external_id, name, kind in result.tuples()
        }
