"""Fact revocation and its impact on decisions (Phase 17).

    FactVersion ──in──► ContextSnapshot ──used by──► Decision  ⇒  impact

Revoking says a version was *wrong* (not merely superseded). In one
transaction it:

1. checks ``facts:write`` and ``decisions:read`` and that the caller may see the
   version (otherwise it does not exist for them);
2. locks the fact (serialized with version writes) and refuses a second
   revocation of the same version;
3. stores the revocation and, for every decision whose frozen context held the
   version, an impact row (``relied_on`` when the decision cited it). Database
   triggers emit ``fact.revoked`` and ``decision.impacted`` through the outbox.

Reading an impact report recomputes it from the database, so it also lists a
decision recorded after the revocation from a context captured before it
(``decided_after_revocation``), which the stored rows could not know about.
Everything here is deterministic SQL; no model is involved.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.facts import PrivacyScope
from app.domain.retrieval import visible_privacy_scopes
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.models.decision import ContextSnapshotFact, Decision, DecisionFact
from app.models.entity import Entity
from app.models.fact import Fact, FactVersion
from app.models.revocation import FactRevocation, RevocationImpact
from app.repositories.facts import FactRepository
from app.services.authorization import require_permission
from app.services.temporal import to_snapshot
from app.temporal.model import VersionSnapshot

MAX_REASON_CHARS = 2000
MAX_LIST = 100


@dataclass(frozen=True, slots=True)
class ImpactedDecision:
    decision_id: UUID
    snapshot_id: UUID
    action: str
    agent: str | None
    decided_at: datetime
    relied_on: bool  # the decision cited the revoked version (not only had it in context)
    recorded_at_revocation: bool  # listed in the immutable impact rows
    decided_after_revocation: bool  # recorded after the revocation, from an older context


@dataclass(frozen=True, slots=True)
class RevocationReport:
    revocation_id: UUID
    fact_id: UUID
    entity_type: str
    external_id: str
    property: str
    version: VersionSnapshot
    reason: str
    revoked_by_user_id: UUID
    revoked_at: datetime
    decisions: list[ImpactedDecision]
    decisions_relied_on: int
    decisions_with_version_in_context: int


class RevocationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def revoke(
        self,
        ctx: TenantContext,
        fact_id: UUID,
        *,
        reason: str,
        version_id: UUID | None = None,
    ) -> RevocationReport:
        reason = reason.strip()
        if not reason or len(reason) > MAX_REASON_CHARS:
            raise ValidationFailedError(f"reason must be 1-{MAX_REASON_CHARS} characters")
        async with self._session.begin():
            role = await require_permission(self._session, ctx, Permission.WRITE_FACTS)
            await require_permission(self._session, ctx, Permission.READ_DECISIONS)
            repository = FactRepository(self._session, ctx.organization_id)
            if await repository.get_fact(fact_id) is None:
                raise NotFoundError("fact not found")
            await repository.lock_fact(fact_id)
            version = (
                await repository.latest_version(fact_id)
                if version_id is None
                else await repository.get_version(version_id)
            )
            visible = visible_privacy_scopes(role, ctx.max_privacy_scope)
            if (
                version is None
                or version.fact_id != fact_id
                or PrivacyScope(version.privacy_scope) not in visible
            ):
                raise NotFoundError("fact version not found")
            existing = await self._session.scalar(
                select(FactRevocation.id).where(
                    FactRevocation.organization_id == ctx.organization_id,
                    FactRevocation.fact_version_id == version.id,
                )
            )
            if existing is not None:
                raise ConflictError("this fact version is already revoked")

            revocation = FactRevocation(
                organization_id=ctx.organization_id,
                fact_version_id=version.id,
                reason=reason,
                revoked_by_user_id=ctx.user_id,
                agent_client_id=ctx.agent_client_id,
            )
            self._session.add(revocation)
            await self._session.flush()
            for decision, relied_on in await self._decisions_with(ctx.organization_id, version.id):
                self._session.add(
                    RevocationImpact(
                        organization_id=ctx.organization_id,
                        revocation_id=revocation.id,
                        decision_id=decision.id,
                        snapshot_id=decision.snapshot_id,
                        fact_version_id=version.id,
                        relied_on=relied_on,
                    )
                )
            await self._session.flush()
            return await self._report(ctx.organization_id, revocation)

    async def impact_of_fact(self, ctx: TenantContext, fact_id: UUID) -> list[RevocationReport]:
        """A report for every revoked version of the fact that the caller may see."""
        async with self._session.begin():
            visible = await self._readable(ctx)
            if await FactRepository(self._session, ctx.organization_id).get_fact(fact_id) is None:
                raise NotFoundError("fact not found")
            rows = await self._session.scalars(
                select(FactRevocation)
                .join(
                    FactVersion,
                    and_(
                        FactVersion.organization_id == FactRevocation.organization_id,
                        FactVersion.id == FactRevocation.fact_version_id,
                    ),
                )
                .where(
                    FactRevocation.organization_id == ctx.organization_id,
                    FactVersion.fact_id == fact_id,
                    FactVersion.privacy_scope.in_(visible),
                )
                .order_by(FactRevocation.revoked_at)
            )
            return [await self._report(ctx.organization_id, r) for r in rows.all()]

    async def impact_of_version(self, ctx: TenantContext, version_id: UUID) -> RevocationReport:
        async with self._session.begin():
            visible = await self._readable(ctx)
            revocation = await self._session.scalar(
                select(FactRevocation)
                .join(
                    FactVersion,
                    and_(
                        FactVersion.organization_id == FactRevocation.organization_id,
                        FactVersion.id == FactRevocation.fact_version_id,
                    ),
                )
                .where(
                    FactRevocation.organization_id == ctx.organization_id,
                    FactRevocation.fact_version_id == version_id,
                    FactVersion.privacy_scope.in_(visible),
                )
            )
            if revocation is None:
                raise NotFoundError("revocation not found")
            return await self._report(ctx.organization_id, revocation)

    async def list_revocations(
        self, ctx: TenantContext, *, limit: int = 20
    ) -> list[RevocationReport]:
        if not 1 <= limit <= MAX_LIST:
            raise ValidationFailedError(f"limit must be between 1 and {MAX_LIST}")
        async with self._session.begin():
            visible = await self._readable(ctx)
            rows = await self._session.scalars(
                select(FactRevocation)
                .join(
                    FactVersion,
                    and_(
                        FactVersion.organization_id == FactRevocation.organization_id,
                        FactVersion.id == FactRevocation.fact_version_id,
                    ),
                )
                .where(
                    FactRevocation.organization_id == ctx.organization_id,
                    FactVersion.privacy_scope.in_(visible),
                )
                .order_by(FactRevocation.revoked_at.desc(), FactRevocation.id)
                .limit(limit)
            )
            return [await self._report(ctx.organization_id, r) for r in rows.all()]

    # --- helpers --------------------------------------------------------------------------

    async def _readable(self, ctx: TenantContext) -> list[PrivacyScope]:
        role = await require_permission(self._session, ctx, Permission.READ_FACTS)
        await require_permission(self._session, ctx, Permission.READ_DECISIONS)
        return list(visible_privacy_scopes(role, ctx.max_privacy_scope))

    async def _decisions_with(
        self, organization_id: UUID, version_id: UUID
    ) -> list[tuple[Decision, bool]]:
        """Decisions whose frozen context contained the version, oldest first."""
        relied_on = exists().where(
            DecisionFact.organization_id == Decision.organization_id,
            DecisionFact.decision_id == Decision.id,
            DecisionFact.fact_version_id == version_id,
        )
        result = await self._session.execute(
            select(Decision, relied_on.label("relied_on"))
            .join(
                ContextSnapshotFact,
                and_(
                    ContextSnapshotFact.organization_id == Decision.organization_id,
                    ContextSnapshotFact.snapshot_id == Decision.snapshot_id,
                ),
            )
            .where(
                Decision.organization_id == organization_id,
                ContextSnapshotFact.fact_version_id == version_id,
            )
            .order_by(Decision.decided_at, Decision.id)
        )
        return [(decision, bool(flag)) for decision, flag in result.tuples()]

    async def _report(self, organization_id: UUID, revocation: FactRevocation) -> RevocationReport:
        version, fact, entity = (
            await self._session.execute(
                select(FactVersion, Fact, Entity)
                .join(
                    Fact,
                    and_(
                        Fact.organization_id == FactVersion.organization_id,
                        Fact.id == FactVersion.fact_id,
                    ),
                )
                .join(
                    Entity,
                    and_(
                        Entity.organization_id == Fact.organization_id,
                        Entity.id == Fact.entity_id,
                    ),
                )
                .where(
                    FactVersion.organization_id == organization_id,
                    FactVersion.id == revocation.fact_version_id,
                )
            )
        ).one()
        stored = set(
            (
                await self._session.scalars(
                    select(RevocationImpact.decision_id).where(
                        RevocationImpact.organization_id == organization_id,
                        RevocationImpact.revocation_id == revocation.id,
                    )
                )
            ).all()
        )
        decisions = [
            ImpactedDecision(
                decision_id=decision.id,
                snapshot_id=decision.snapshot_id,
                action=decision.action,
                agent=decision.agent,
                decided_at=decision.decided_at,
                relied_on=relied_on,
                recorded_at_revocation=decision.id in stored,
                decided_after_revocation=decision.id not in stored,
            )
            for decision, relied_on in await self._decisions_with(organization_id, version.id)
        ]
        return RevocationReport(
            revocation_id=revocation.id,
            fact_id=fact.id,
            entity_type=entity.entity_type,
            external_id=entity.external_id,
            property=fact.property,
            version=to_snapshot(version),
            reason=revocation.reason,
            revoked_by_user_id=revocation.revoked_by_user_id,
            revoked_at=revocation.revoked_at,
            decisions=decisions,
            decisions_relied_on=sum(d.relied_on for d in decisions),
            decisions_with_version_in_context=len(decisions),
        )
