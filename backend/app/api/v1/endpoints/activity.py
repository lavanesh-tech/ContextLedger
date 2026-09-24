"""Tenant activity counters (maintained from Kafka events)."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import SessionDep, TenantDep
from app.api.errors import problem_responses
from app.services.activity import MAX_DAYS, ActivityReport, ActivityService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["activity"])


@router.get(
    "/activity",
    summary="Daily activity (eventually consistent)",
    responses=problem_responses(401, 403, 422),
)
async def activity(
    ctx: TenantDep,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30,
) -> ActivityReport:
    """Facts recorded, evidence captured and decisions recorded per UTC day, counted by
    the `activity-projector` Kafka consumer. `pending_events` are not counted yet."""
    return await ActivityService(session).recent(ctx, days=days)
