"""Contradictions between fact versions: list, inspect, resolve, optional LLM review."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field

from app.api.dependencies import GeneratorDep, SessionDep, SettingsDep, TenantDep
from app.api.errors import problem_responses
from app.domain.contradictions import ContradictionStatus
from app.schemas.api import Request
from app.services.contradictions import (
    MAX_LIST,
    ContradictionService,
    ContradictionView,
    ReviewResult,
)

router = APIRouter(prefix="/organizations/{organization_id}", tags=["contradictions"])


class ContradictionUpdate(Request):
    status: Literal["resolved", "dismissed"]
    note: str | None = Field(default=None, max_length=2000)


@router.get(
    "/contradictions",
    summary="Contradictions you may see",
    responses=problem_responses(401, 403, 422),
)
async def list_contradictions(
    ctx: TenantDep,
    session: SessionDep,
    status: ContradictionStatus | None = None,
    entity_type: str | None = None,
    external_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIST),
) -> list[ContradictionView]:
    """Newest first. Hidden when either version is above your privacy ceiling."""
    return await ContradictionService(session).list_contradictions(
        ctx, status=status, entity_type=entity_type, external_id=external_id, limit=limit
    )


@router.get(
    "/contradictions/{contradiction_id}",
    summary="One contradiction, with both versions",
    responses=problem_responses(401, 403, 404),
)
async def get_contradiction(
    contradiction_id: UUID, ctx: TenantDep, session: SessionDep
) -> ContradictionView:
    return await ContradictionService(session).get(ctx, contradiction_id)


@router.patch(
    "/contradictions/{contradiction_id}",
    summary="Resolve or dismiss a contradiction",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def update_contradiction(
    contradiction_id: UUID, body: ContradictionUpdate, ctx: TenantDep, session: SessionDep
) -> ContradictionView:
    """Records who closed it and why. Fact versions are never changed or deleted."""
    return await ContradictionService(session).resolve(
        ctx, contradiction_id, status=ContradictionStatus(body.status), note=body.note
    )


@router.post(
    "/entities/{entity_type}/{external_id}/contradiction-review",
    summary="Ask the LLM to suggest semantic contradictions between an entity's facts",
    responses=problem_responses(401, 403, 404, 429, 503),
)
async def review_entity(
    entity_type: str,
    external_id: str,
    ctx: TenantDep,
    session: SessionDep,
    generator: GeneratorDep,
    settings: SettingsDep,
) -> ReviewResult:
    """Optional assistance for conflicts between different properties (e.g. an ACTIVE
    status and a closed account). Only facts you may see are sent; every suggestion must
    name two of them, and is stored as an open `semantic` contradiction for a person to
    confirm or dismiss. Needs `facts:read` and `facts:write`."""
    return await ContradictionService(session).review_entity(
        ctx,
        generator,
        entity_type=entity_type,
        external_id=external_id,
        max_output_tokens=settings.llm_max_output_tokens,
    )
