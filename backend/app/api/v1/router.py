"""Aggregates every v1 endpoint router."""

from fastapi import APIRouter

from app.api.v1.endpoints import decisions, facts, health, organizations, provenance, search, users

api_router = APIRouter()
api_router.include_router(health.router)
for module in (users, organizations, facts, search, decisions, provenance):
    api_router.include_router(module.router)
