"""Contradictions: list, inspect, resolve, and an optional LLM review.

Deterministic detection happens when a fact version is recorded
(``FactService.record_version``). This service reads and manages the results.

Visibility: a contradiction is shown only if the reader may see both versions
(``privacy_scope`` is the more sensitive of the two), inside the reader's
organization, with ``facts:read``. Resolving needs ``facts:write``.

The LLM review (``review_entity``) is optional assistance for contradictions
between *different* properties, which no deterministic rule can see. It gets
only the entity's current facts the caller may see, labelled F1..Fn; every
finding must name two supplied labels of two different facts or it is
rejected. Accepted findings are stored as ``semantic`` contradictions with the
prompt version as their detector, for a person to confirm or dismiss.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.orchestration.chains import structured_chain
from app.ai.orchestration.chat_model import ContextLedgerChatModel
from app.ai.prompts import get_prompt
from app.ai.providers import GenerationError, GenerationProvider, MalformedGenerationError
from app.domain.contradictions import ContradictionKind, ContradictionStatus
from app.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.facts import PrivacyScope
from app.domain.retrieval import PRIVACY_ORDER, visible_privacy_scopes
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.models.contradiction import Contradiction
from app.models.entity import Entity
from app.models.fact import Fact, FactVersion
from app.models.source import FactSource
from app.services.answers import AnswerGenerationError
from app.services.authorization import require_permission
from app.services.temporal import TemporalService, to_snapshot
from app.temporal.model import ResolvedFact, VersionSnapshot

REVIEW_PROMPT = "contradiction-review-v1"
MAX_LIST = 200
MAX_REVIEW_FACTS = 50
MAX_VALUE_CHARS = 300


@dataclass(frozen=True, slots=True)
class ContradictionSide:
    fact_id: UUID
    property: str
    source_id: UUID
    source_name: str
    version: VersionSnapshot


@dataclass(frozen=True, slots=True)
class ContradictionView:
    id: UUID
    entity_type: str
    external_id: str
    kind: ContradictionKind
    detector: str
    explanation: str
    status: ContradictionStatus
    preferred_version_id: UUID | None
    privacy_scope: PrivacyScope
    left: ContradictionSide
    right: ContradictionSide
    detected_at: datetime
    resolved_at: datetime | None
    resolved_by_user_id: UUID | None
    resolution_note: str | None


@dataclass(frozen=True, slots=True)
class ReviewResult:
    entity_type: str
    external_id: str
    facts_reviewed: int
    created: list[ContradictionView]
    already_known: int
    rejected_findings: int  # findings naming unknown labels, or the same fact twice
    prompt_version: str
    model: str | None  # None: nothing to review, no model call


class ContradictionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_contradictions(
        self,
        ctx: TenantContext,
        *,
        status: ContradictionStatus | None = None,
        entity_type: str | None = None,
        external_id: str | None = None,
        limit: int = 50,
    ) -> list[ContradictionView]:
        if not 1 <= limit <= MAX_LIST:
            raise ValidationFailedError(f"limit must be between 1 and {MAX_LIST}")
        async with self._session.begin():
            visible = await self._visible(ctx, Permission.READ_FACTS)
            query = (
                select(Contradiction)
                .join(
                    Entity,
                    (Entity.organization_id == Contradiction.organization_id)
                    & (Entity.id == Contradiction.entity_id),
                )
                .where(
                    Contradiction.organization_id == ctx.organization_id,
                    Contradiction.privacy_scope.in_(visible),
                )
                .order_by(Contradiction.detected_at.desc(), Contradiction.id)
                .limit(limit)
            )
            if status is not None:
                query = query.where(Contradiction.status == status)
            if entity_type is not None:
                query = query.where(Entity.entity_type == entity_type)
            if external_id is not None:
                query = query.where(Entity.external_id == external_id)
            rows = list((await self._session.scalars(query)).all())
            return [await self._view(row) for row in rows]

    async def get(self, ctx: TenantContext, contradiction_id: UUID) -> ContradictionView:
        async with self._session.begin():
            visible = await self._visible(ctx, Permission.READ_FACTS)
            row = await self._row(ctx, contradiction_id, visible)
            return await self._view(row)

    async def resolve(
        self,
        ctx: TenantContext,
        contradiction_id: UUID,
        *,
        status: ContradictionStatus,
        note: str | None = None,
    ) -> ContradictionView:
        """Close an open contradiction as resolved (the data was reconciled or one source
        was right) or dismissed (not a real conflict). Facts are never changed here."""
        if status is ContradictionStatus.OPEN:
            raise ValidationFailedError("status must be resolved or dismissed")
        async with self._session.begin():
            visible = await self._visible(ctx, Permission.WRITE_FACTS)
            row = await self._row(ctx, contradiction_id, visible, lock=True)
            if row.status is not ContradictionStatus.OPEN:
                raise ConflictError(f"contradiction is already {row.status.value}")
            row.status = status
            row.resolution_note = note
            row.resolved_by_user_id = ctx.user_id
            row.resolved_at = datetime.now(UTC)
            await self._session.flush()
            return await self._view(row)

    async def review_entity(
        self,
        ctx: TenantContext,
        generator: GenerationProvider,
        *,
        entity_type: str,
        external_id: str,
        max_output_tokens: int = 800,
    ) -> ReviewResult:
        async with self._session.begin():
            role = await require_permission(self._session, ctx, Permission.READ_FACTS)
            await require_permission(self._session, ctx, Permission.WRITE_FACTS)
        visible = visible_privacy_scopes(role, ctx.max_privacy_scope)
        facts = [
            f
            for f in await TemporalService(self._session).facts_at(
                ctx, entity_type=entity_type, external_id=external_id
            )
            if PrivacyScope(f.version.privacy_scope) in visible
        ][:MAX_REVIEW_FACTS]
        prompt = get_prompt(REVIEW_PROMPT)
        if len(facts) < 2:
            return ReviewResult(
                entity_type, external_id, len(facts), [], 0, 0, prompt.version, None
            )

        labelled = {f"F{i}": fact for i, fact in enumerate(facts, start=1)}
        chain = structured_chain(
            prompt,
            ContextLedgerChatModel(provider=generator, max_output_tokens=max_output_tokens),
            schema_name="contradiction_review",
        )
        try:
            message = await chain.ainvoke(
                {"entity": f"{entity_type}:{external_id}", "facts": _render(labelled)}
            )
            findings = _parse_findings(str(message.content))
        except GenerationError as exc:
            raise AnswerGenerationError(
                "the model could not review the facts", cause=type(exc).__name__
            ) from exc
        model_id = str(message.response_metadata.get("model") or generator.model_id)

        accepted: list[tuple[ResolvedFact, ResolvedFact, str]] = []
        rejected = 0
        for left_label, right_label, explanation in findings:
            left, right = labelled.get(left_label), labelled.get(right_label)
            if left is None or right is None or left.fact_id == right.fact_id:
                rejected += 1
                continue
            accepted.append((left, right, explanation))

        created: list[ContradictionView] = []
        known = 0
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.WRITE_FACTS)
            for left, right, explanation in accepted:
                first, second = sorted((left, right), key=lambda f: str(f.version.id))
                existing = await self._session.scalar(
                    select(Contradiction.id).where(
                        Contradiction.organization_id == ctx.organization_id,
                        Contradiction.left_version_id == first.version.id,
                        Contradiction.right_version_id == second.version.id,
                    )
                )
                if existing is not None:
                    known += 1
                    continue
                row = Contradiction(
                    organization_id=ctx.organization_id,
                    entity_id=first.entity_id,
                    left_version_id=first.version.id,
                    right_version_id=second.version.id,
                    kind=ContradictionKind.SEMANTIC,
                    detector=f"llm:{prompt.version}",
                    explanation=explanation[:2000],
                    preferred_version_id=None,  # a person decides
                    privacy_scope=max(
                        PrivacyScope(first.version.privacy_scope),
                        PrivacyScope(second.version.privacy_scope),
                        key=PRIVACY_ORDER.index,
                    ),
                    status=ContradictionStatus.OPEN,
                )
                self._session.add(row)
                await self._session.flush()
                created.append(await self._view(row))
        return ReviewResult(
            entity_type,
            external_id,
            len(facts),
            created,
            known,
            rejected,
            prompt.version,
            model_id,
        )

    # --- helpers --------------------------------------------------------------------------

    async def _visible(self, ctx: TenantContext, permission: Permission) -> list[PrivacyScope]:
        role = await require_permission(self._session, ctx, permission)
        return sorted(visible_privacy_scopes(role, ctx.max_privacy_scope), key=PRIVACY_ORDER.index)

    async def _row(
        self,
        ctx: TenantContext,
        contradiction_id: UUID,
        visible: list[PrivacyScope],
        *,
        lock: bool = False,
    ) -> Contradiction:
        query = select(Contradiction).where(
            Contradiction.organization_id == ctx.organization_id,
            Contradiction.id == contradiction_id,
            Contradiction.privacy_scope.in_(visible),
        )
        if lock:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        if row is None:
            raise NotFoundError("contradiction not found")
        return row

    async def _side(
        self, organization_id: UUID, version_id: UUID
    ) -> tuple[ContradictionSide, Entity]:
        version, fact, entity, source = (
            await self._session.execute(
                select(FactVersion, Fact, Entity, FactSource)
                .join(
                    Fact,
                    (Fact.organization_id == FactVersion.organization_id)
                    & (Fact.id == FactVersion.fact_id),
                )
                .join(
                    Entity,
                    (Entity.organization_id == Fact.organization_id)
                    & (Entity.id == Fact.entity_id),
                )
                .join(
                    FactSource,
                    (FactSource.organization_id == FactVersion.organization_id)
                    & (FactSource.id == FactVersion.source_id),
                )
                .where(FactVersion.organization_id == organization_id, FactVersion.id == version_id)
            )
        ).one()
        side = ContradictionSide(
            fact_id=fact.id,
            property=fact.property,
            source_id=source.id,
            source_name=source.name,
            version=to_snapshot(version),
        )
        return side, entity

    async def _view(self, row: Contradiction) -> ContradictionView:
        left, entity = await self._side(row.organization_id, row.left_version_id)
        right, _ = await self._side(row.organization_id, row.right_version_id)
        return ContradictionView(
            id=row.id,
            entity_type=entity.entity_type,
            external_id=entity.external_id,
            kind=row.kind,
            detector=row.detector,
            explanation=row.explanation,
            status=row.status,
            preferred_version_id=row.preferred_version_id,
            privacy_scope=row.privacy_scope,
            left=left,
            right=right,
            detected_at=row.detected_at,
            resolved_at=row.resolved_at,
            resolved_by_user_id=row.resolved_by_user_id,
            resolution_note=row.resolution_note,
        )


def _render(labelled: dict[str, ResolvedFact]) -> str:
    lines = []
    for label, fact in labelled.items():
        value = json.dumps(fact.version.value, default=str)
        if len(value) > MAX_VALUE_CHARS:
            value = value[:MAX_VALUE_CHARS] + "..."
        lines.append(f"[{label}] {fact.property} = {value}")
    return "\n".join(lines)


def _parse_findings(text: str) -> list[tuple[str, str, str]]:
    try:
        data: Any = json.loads(text)
        return [
            (str(item["left"]), str(item["right"]), str(item["explanation"]))
            for item in data["findings"]
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise MalformedGenerationError("the model returned malformed findings") from exc
