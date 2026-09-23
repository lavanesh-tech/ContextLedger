"""FastAPI application factory.

Run locally with::

    uvicorn app.main:create_app --factory --reload --no-access-log

A factory (instead of a module-level ``app``) means importing this module has
no side effects, and tests can build isolated apps with their own settings.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.v1.router import api_router
from app.core.config import API_V1_PREFIX, Settings, get_settings
from app.core.correlation import CorrelationIdMiddleware
from app.core.logging import configure_logging

logger = logging.getLogger("contextledger")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "application.startup",
        extra={"environment": settings.environment.value, "version": __version__},
    )
    yield
    logger.info("application.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary="MCP-native temporal RAG and decision-provenance platform for AI agents.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(CorrelationIdMiddleware, header_name=settings.correlation_id_header)
    app.include_router(api_router, prefix=API_V1_PREFIX)
    return app
