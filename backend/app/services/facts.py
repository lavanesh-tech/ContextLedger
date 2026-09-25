"""Recording temporal facts.

This service is the single write path for fact versions. The REST API
(Phase 12) and the MCP server (Phase 11) both call it, so both enforce the
same rules. Temporal *queries* (valid at T, as known at T, history diffs)
live in the temporal resolution engine (Phase 5).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.contradictions import (
    RULE_OBSERVED_VALUE_CONFLICT,
    ContradictionKind,
    ContradictionStatus,
    VersionFacts,
    preferred,
    value_conflict,
)
from app.domain.errors import ConflictError, NotFoundError
from app.domain.evidence import EvidenceRelation
from app.domain.facts import (
    LatestVersion,
    PrivacyScope,
    SourceType,
    ValidityWindow,
    plan_new_version,
    require_aware,
)
from app.domain.retrieval import PRIVACY_ORDER
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.domain.validation import (
    normalize_confidence,
    normalize_external_id,
    normalize_identifier,
    normalize_name,
    normalize_uri,
    validate_authority,
    validate_fact_value,
)
from app.models.contradiction import Contradiction
from app.models.fact import Fact, FactVersion
from app.models.source import FactSource
from app.repositories.facts import FactRepository
from app.services.authorization import require_permission
from app.services.evidence import link_evidence


@dataclass(frozen=True, slots=True)
class RecordFactVersion:
    """Command: "source S says entity E's property P has value V from valid_from"."""

    entity_type: str
    external_id: str
    property: str
    value: Any
    source_id: UUID
    valid_from: datetime
    valid_until: datetime | None = None
    observed_at: datetime | None = None
    authority: int | None = None  # defaults to the source's default_authority
    confidence: Decimal | float | str = field(default=Decimal("1.000"))
    privacy_scope: PrivacyScope = PrivacyScope.INTERNAL
    # Evidence (already captured) that supports this version; linked atomically.
    evidence_ids: tuple[UUID, ...] = ()


class FactService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register_source(
        self,
        ctx: TenantContext,
        *,
        name: str,
        source_type: SourceType,
        uri: str | None = None,
        default_authority: int = 50,
    ) -> FactSource:
        normalized_name = normalize_name(name, field="name")
        normalized_uri = normalize_uri(uri)
        authority = validate_authority(default_authority)

        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.WRITE_FACTS)
            repository = FactRepository(self._session, ctx.organization_id)
            if await repository.get_source_by_name(normalized_name) is not None:
                raise ConflictError("a source with this name already exists")
            source = repository.new_source(
                name=normalized_name,
                source_type=SourceType(source_type),
                uri=normalized_uri,
                default_authority=authority,
            )
            try:
                await self._session.flush()
            except IntegrityError as exc:
                raise ConflictError("a source with this name already exists") from exc
        return source

    async def record_version(self, ctx: TenantContext, command: RecordFactVersion) -> FactVersion:
        """Append a version, closing the previous open version when needed.

        Steps, all in one transaction:
        1. authorize (current membership, WRITE_FACTS);
        2. find or create the entity and fact (race-safe upserts);
        3. lock the fact row, so concurrent writes to one fact run in order;
        4. plan the new version from the latest one (domain rules);
        5. take one transaction timestamp (strictly after the previous version's);
        6. close the previous version if it was open, then insert the new one.
        The exclusion constraint and append-only trigger back this up in the DB.
        """
        entity_type = normalize_identifier(command.entity_type, field="entity_type")
        external_id = normalize_external_id(command.external_id)
        property_name = normalize_identifier(command.property, field="property")
        value = validate_fact_value(command.value)
        window = ValidityWindow(command.valid_from, command.valid_until)
        observed_at = require_aware(command.observed_at or command.valid_from, field="observed_at")
        confidence = normalize_confidence(command.confidence)
        privacy_scope = PrivacyScope(command.privacy_scope)

        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.WRITE_FACTS)
            repository = FactRepository(self._session, ctx.organization_id)

            source = await repository.get_source(command.source_id)
            if source is None:
                raise NotFoundError("source not found")
            authority = validate_authority(
                source.default_authority if command.authority is None else command.authority
            )

            entity = await repository.get_or_create_entity(
                entity_type=entity_type, external_id=external_id
            )
            fact = await repository.get_or_create_fact(
                entity_id=entity.id, property_name=property_name
            )
            await repository.lock_fact(fact.id)

            latest = await repository.latest_version(fact.id)
            plan = plan_new_version(
                None
                if latest is None
                else LatestVersion(
                    version=latest.version,
                    window=ValidityWindow(latest.valid_from, latest.valid_until),
                ),
                window,
            )
            # Transaction time is taken *after* the lock and is strictly increasing
            # per fact, so "what was known at K" always sees a prefix of the chain.
            recorded_at = await repository.next_transaction_time(latest)
            # The previous version as it was claimed, before this write closes it.
            previous_claim = None if latest is None else _rule_view(latest)
            if latest is not None and plan.close_previous_at is not None:
                await repository.close_version(
                    latest, plan.close_previous_at, recorded_at=recorded_at
                )

            fact_version = repository.new_version(
                fact_id=fact.id,
                version=plan.version,
                value=value,
                source_id=source.id,
                valid_from=window.valid_from,
                valid_until=window.valid_until,
                observed_at=observed_at,
                supersedes_id=None if latest is None else latest.id,
                authority=authority,
                confidence=confidence,
                privacy_scope=privacy_scope,
                recorded_at=recorded_at,
            )
            await self._session.flush()
            if latest is not None and previous_claim is not None:
                self._detect_value_conflict(ctx, entity.id, latest, previous_claim, fact_version)
            # Same transaction: if any evidence id is unknown or foreign, the
            # version itself is rolled back too.
            await link_evidence(
                self._session,
                ctx,
                fact_version_id=fact_version.id,
                evidence_ids=command.evidence_ids,
                relation=EvidenceRelation.SUPPORTS,
            )
        return fact_version

    def _detect_value_conflict(
        self,
        ctx: TenantContext,
        entity_id: UUID,
        previous: FactVersion,
        left: VersionFacts,
        new: FactVersion,
    ) -> None:
        """Record a contradiction in the same transaction as the version that caused it
        (rule in app/domain/contradictions.py). Both versions are kept either way."""
        right = _rule_view(new)
        reason = value_conflict(left, right)
        if reason is None:
            return
        self._session.add(
            Contradiction(
                organization_id=ctx.organization_id,
                entity_id=entity_id,
                left_version_id=previous.id,
                right_version_id=new.id,
                kind=ContradictionKind.VALUE_CONFLICT,
                detector=RULE_OBSERVED_VALUE_CONFLICT,
                explanation=reason,
                preferred_version_id=preferred(left, right),
                privacy_scope=max(
                    PrivacyScope(previous.privacy_scope),
                    PrivacyScope(new.privacy_scope),
                    key=PRIVACY_ORDER.index,
                ),
                status=ContradictionStatus.OPEN,
            )
        )

    async def get_version(self, ctx: TenantContext, version_id: UUID) -> FactVersion:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            version = await FactRepository(self._session, ctx.organization_id).get_version(
                version_id
            )
            if version is None:
                raise NotFoundError("fact version not found")
            return version

    async def get_fact(self, ctx: TenantContext, fact_id: UUID) -> Fact:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            fact = await FactRepository(self._session, ctx.organization_id).get_fact(fact_id)
            if fact is None:
                raise NotFoundError("fact not found")
            return fact

    async def list_versions(self, ctx: TenantContext, fact_id: UUID) -> Sequence[FactVersion]:
        """All versions of one fact, oldest first. Unknown or foreign facts -> NotFound."""
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            repository = FactRepository(self._session, ctx.organization_id)
            if await repository.get_fact(fact_id) is None:
                raise NotFoundError("fact not found")
            return await repository.list_versions(fact_id)


def _rule_view(version: FactVersion) -> VersionFacts:
    return VersionFacts(
        id=version.id,
        source_id=version.source_id,
        value=version.value,
        valid_from=version.valid_from,
        valid_until=version.valid_until,
        observed_at=version.observed_at,
        authority=version.authority,
        confidence=version.confidence,
    )
