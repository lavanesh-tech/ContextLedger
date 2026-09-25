"""Aggregates every v1 endpoint router."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    activity,
    answers,
    auth,
    contradictions,
    decisions,
    facts,
    health,
    investigations,
    organizations,
    provenance,
    revocations,
    search,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
for module in (
    auth,
    users,
    organizations,
    facts,
    contradictions,
    revocations,
    search,
    decisions,
    provenance,
    activity,
    answers,
    investigations,
):
    api_router.include_router(module.router)
