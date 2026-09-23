"""Impact analysis and lineage over the Neo4j provenance graph (503 if not configured)."""

from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import GraphDep, SessionDep, TenantDep
from app.api.errors import problem_responses
from app.services.provenance import DecisionLineage, ImpactReport, ProvenanceService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["provenance"])

RESPONSES = problem_responses(401, 403, 404, 503)


@router.get(
    "/impact/fact-versions/{version_id}",
    summary="Decisions depending on a fact version",
    responses=RESPONSES,
)
async def impact_of_fact_version(
    version_id: UUID, ctx: TenantDep, session: SessionDep, graph: GraphDep
) -> ImpactReport:
    return await ProvenanceService(session, graph).impact_of_fact_version(ctx, version_id)


@router.get(
    "/impact/sources/{source_id}",
    summary="Versions and decisions depending on a source",
    responses=RESPONSES,
)
async def impact_of_source(
    source_id: UUID, ctx: TenantDep, session: SessionDep, graph: GraphDep
) -> ImpactReport:
    return await ProvenanceService(session, graph).impact_of_source(ctx, source_id)


@router.get(
    "/impact/evidence/{evidence_id}",
    summary="Versions and decisions depending on a piece of evidence",
    responses=RESPONSES,
)
async def impact_of_evidence(
    evidence_id: UUID, ctx: TenantDep, session: SessionDep, graph: GraphDep
) -> ImpactReport:
    return await ProvenanceService(session, graph).impact_of_evidence(ctx, evidence_id)


@router.get(
    "/decisions/{decision_id}/lineage",
    summary="What a decision rests on",
    responses=RESPONSES,
)
async def decision_lineage(
    decision_id: UUID, ctx: TenantDep, session: SessionDep, graph: GraphDep
) -> DecisionLineage:
    return await ProvenanceService(session, graph).decision_lineage(ctx, decision_id)
