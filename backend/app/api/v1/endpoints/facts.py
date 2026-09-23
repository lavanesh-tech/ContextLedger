"""Sources, bitemporal facts, evidence and provenance.

Read endpoints apply the caller's privacy ceiling (ADR-023): facts above it are
withheld, and the response says how many were withheld.
"""

from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import AwareDatetime

from app.api.dependencies import SessionDep, TenantDep
from app.api.errors import problem_responses
from app.domain.errors import NotFoundError
from app.domain.facts import PrivacyScope
from app.domain.retrieval import visible_privacy_scopes
from app.domain.tenancy import TenantContext
from app.schemas.api import (
    AttachResult,
    CapturedEvidenceOut,
    EntityFacts,
    EntityTimeline,
    EvidenceAttach,
    EvidenceCreate,
    EvidenceOut,
    FactVersionCreate,
    FactVersionOut,
    LinkedEvidenceOut,
    ProvenanceOut,
    SourceCreate,
    SourceOut,
)
from app.services.evidence import EvidenceService
from app.services.facts import FactService, RecordFactVersion
from app.services.temporal import TemporalService
from app.temporal.model import FactChange, Lineage, VersionSnapshot

router = APIRouter(prefix="/organizations/{organization_id}")

READ = problem_responses(401, 403, 404, 422)
WRITE = problem_responses(401, 403, 404, 409, 422)


def _visible(ctx: TenantContext) -> frozenset[PrivacyScope]:
    return visible_privacy_scopes(ctx.role)


def _all_visible(versions: list[VersionSnapshot | None], ctx: TenantContext) -> bool:
    visible = _visible(ctx)
    return all(v is None or v.privacy_scope in visible for v in versions)


# --- sources ----------------------------------------------------------------------------


@router.post(
    "/sources",
    tags=["sources"],
    status_code=status.HTTP_201_CREATED,
    summary="Register a source",
    responses=WRITE,
)
async def register_source(body: SourceCreate, ctx: TenantDep, session: SessionDep) -> SourceOut:
    source = await FactService(session).register_source(ctx, **body.model_dump())
    return SourceOut.model_validate(source)


@router.get("/sources", tags=["sources"], summary="List sources", responses=READ)
async def list_sources(ctx: TenantDep, session: SessionDep) -> list[SourceOut]:
    return [SourceOut.model_validate(s) for s in await EvidenceService(session).list_sources(ctx)]


@router.get("/sources/{source_id}", tags=["sources"], summary="Get a source", responses=READ)
async def get_source(source_id: UUID, ctx: TenantDep, session: SessionDep) -> SourceOut:
    return SourceOut.model_validate(await EvidenceService(session).get_source(ctx, source_id))


# --- facts ------------------------------------------------------------------------------


@router.post(
    "/facts",
    tags=["facts"],
    status_code=status.HTTP_201_CREATED,
    summary="Record a fact version",
    responses=WRITE,
)
async def record_fact_version(
    body: FactVersionCreate, ctx: TenantDep, session: SessionDep
) -> FactVersionOut:
    """Source S says: entity E's property P has value V from `valid_from`.

    Supersedes the current version when `valid_from` is later; the database
    guarantees versions of one fact never overlap in valid time.
    """
    data = body.model_dump()
    data["evidence_ids"] = tuple(data["evidence_ids"])
    version = await FactService(session).record_version(ctx, RecordFactVersion(**data))
    return FactVersionOut.model_validate(version)


@router.get(
    "/entities/{entity_type}/{external_id}/facts",
    tags=["facts"],
    summary="Facts of an entity at a point in time",
    responses=READ,
)
async def entity_facts(
    entity_type: str,
    external_id: str,
    ctx: TenantDep,
    session: SessionDep,
    valid_at: AwareDatetime | None = None,
    known_at: AwareDatetime | None = None,
) -> EntityFacts:
    """Values valid at `valid_at` (default: now) as known at `known_at` (default: latest)."""
    facts = await TemporalService(session).facts_at(
        ctx, entity_type=entity_type, external_id=external_id, valid_at=valid_at, known_at=known_at
    )
    shown = [f for f in facts if f.version.privacy_scope in _visible(ctx)]
    return EntityFacts(facts=shown, withheld_by_privacy_scope=len(facts) - len(shown))


@router.get(
    "/entities/{entity_type}/{external_id}/timeline",
    tags=["facts"],
    summary="Every version of every fact of an entity",
    responses=READ,
)
async def entity_timeline(
    entity_type: str,
    external_id: str,
    ctx: TenantDep,
    session: SessionDep,
    known_at: AwareDatetime | None = None,
) -> EntityTimeline:
    entries = await TemporalService(session).entity_timeline(
        ctx, entity_type=entity_type, external_id=external_id, known_at=known_at
    )
    shown = [e for e in entries if e.version.privacy_scope in _visible(ctx)]
    return EntityTimeline(entries=shown, withheld_by_privacy_scope=len(entries) - len(shown))


@router.get(
    "/entities/{entity_type}/{external_id}/changes",
    tags=["facts"],
    summary="What changed between two instants",
    responses=READ,
)
async def entity_changes(
    entity_type: str,
    external_id: str,
    start: AwareDatetime,
    end: AwareDatetime,
    ctx: TenantDep,
    session: SessionDep,
    known_at: AwareDatetime | None = None,
) -> list[FactChange]:
    changes = await TemporalService(session).changes_between(
        ctx,
        entity_type=entity_type,
        external_id=external_id,
        start=start,
        end=end,
        known_at=known_at,
    )
    return [c for c in changes if _all_visible([c.before, c.after, *c.transitions], ctx)]


@router.get(
    "/facts/{fact_id}/history",
    tags=["facts"],
    summary="All versions of one fact",
    responses=READ,
)
async def fact_history(
    fact_id: UUID,
    ctx: TenantDep,
    session: SessionDep,
    known_at: AwareDatetime | None = None,
) -> list[VersionSnapshot]:
    history = await TemporalService(session).history(ctx, fact_id, known_at=known_at)
    return [v for v in history if v.privacy_scope in _visible(ctx)]


@router.get(
    "/fact-versions/{version_id}/lineage",
    tags=["facts"],
    summary="What a version superseded and what superseded it",
    responses=READ,
)
async def version_lineage(version_id: UUID, ctx: TenantDep, session: SessionDep) -> Lineage:
    lineage = await TemporalService(session).lineage(ctx, version_id)
    if not _all_visible([lineage.version, *lineage.ancestors, *lineage.descendants], ctx):
        raise NotFoundError("fact version not found")  # do not confirm it exists
    return lineage


# --- evidence ---------------------------------------------------------------------------


@router.post(
    "/evidence",
    tags=["evidence"],
    status_code=status.HTTP_201_CREATED,
    summary="Capture evidence (idempotent by content)",
    responses=WRITE,
)
async def capture_evidence(
    body: EvidenceCreate, ctx: TenantDep, session: SessionDep, response: Response
) -> CapturedEvidenceOut:
    captured = await EvidenceService(session).capture_evidence(ctx, **body.model_dump())
    if not captured.created:
        response.status_code = status.HTTP_200_OK  # same content was already captured
    return CapturedEvidenceOut(
        evidence=EvidenceOut.model_validate(captured.evidence), created=captured.created
    )


@router.post(
    "/fact-versions/{version_id}/evidence",
    tags=["evidence"],
    summary="Link evidence to a fact version",
    responses=WRITE,
)
async def attach_evidence(
    version_id: UUID, body: EvidenceAttach, ctx: TenantDep, session: SessionDep
) -> AttachResult:
    linked = await EvidenceService(session).attach(
        ctx, fact_version_id=version_id, evidence_id=body.evidence_id, relation=body.relation
    )
    return AttachResult(linked=linked)


@router.get(
    "/fact-versions/{version_id}/provenance",
    tags=["evidence"],
    summary="The version, its source and every linked piece of evidence",
    responses=READ,
)
async def version_provenance(
    version_id: UUID, ctx: TenantDep, session: SessionDep
) -> ProvenanceOut:
    provenance = await EvidenceService(session).provenance(ctx, version_id)
    visible = _visible(ctx)
    if provenance.version.privacy_scope not in visible:
        raise NotFoundError("fact version not found")
    shown = [e for e in provenance.evidence if e.evidence.privacy_scope in visible]
    return ProvenanceOut(
        version=FactVersionOut.model_validate(provenance.version),
        source=SourceOut.model_validate(provenance.source),
        evidence=[
            LinkedEvidenceOut(
                evidence=EvidenceOut.model_validate(e.evidence),
                source=SourceOut.model_validate(e.source),
                relation=e.relation,
                linked_at=e.linked_at,
                linked_by_user_id=e.linked_by_user_id,
            )
            for e in shown
        ],
        withheld_evidence=len(provenance.evidence) - len(shown),
    )
