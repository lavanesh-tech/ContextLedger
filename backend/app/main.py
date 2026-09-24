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
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.api.errors import install_error_handlers
from app.api.v1.router import api_router
from app.auth.tokens import TokenService
from app.cache.idempotency import IdempotencyMiddleware
from app.cache.rate_limit import RateLimiter
from app.cache.retrieval import RetrievalCache
from app.cache.store import build_store
from app.core.config import API_V1_PREFIX, Settings, get_settings
from app.core.correlation import CorrelationIdMiddleware
from app.core.logging import configure_logging
from app.db.migrations import expected_schema_revision
from app.db.session import create_engine, create_session_factory
from app.provenance.graph import GraphReader, build_driver
from app.providers.embeddings import build_embedding_provider, build_openai_http_client

logger = logging.getLogger("contextledger")

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and readiness."},
    {"name": "auth", "description": "Access tokens (OAuth2 client credentials) and agents."},
    {"name": "users", "description": "User accounts (global, not tenant-owned)."},
    {"name": "organizations", "description": "Tenants and their members (RBAC)."},
    {"name": "sources", "description": "Where facts and evidence come from."},
    {"name": "facts", "description": "Bitemporal facts: record versions, ask about any time."},
    {"name": "evidence", "description": "Immutable, content-addressed supporting material."},
    {"name": "search", "description": "Hybrid temporal retrieval (vector + full text)."},
    {"name": "decisions", "description": "Frozen context snapshots and sealed decision receipts."},
    {"name": "provenance", "description": "Impact analysis and lineage (Neo4j graph)."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "application.startup",
        extra={
            "environment": settings.environment.value,
            "version": __version__,
            "db_host": settings.db_host,
            "db_name": settings.db_name,
            "expected_schema_revision": app.state.expected_schema_revision,
        },
    )
    try:
        yield
    finally:
        await app.state.store.close()
        if app.state.graph_driver is not None:
            await app.state.graph_driver.close()
        if app.state.http_client is not None:
            await app.state.http_client.aclose()
        engine: AsyncEngine = app.state.db_engine
        await engine.dispose()
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
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Creating the engine does not connect; the first connection is made lazily.
    engine = create_engine(settings)
    app.state.db_engine = engine
    app.state.db_sessionmaker = create_session_factory(engine)
    app.state.expected_schema_revision = expected_schema_revision(settings.alembic_ini_path)

    # Created lazily too: no network I/O happens until a request needs it.
    app.state.http_client = (
        build_openai_http_client(settings) if settings.embedding_provider == "openai" else None
    )
    app.state.embedding_provider = build_embedding_provider(settings, app.state.http_client)
    app.state.graph_driver = (
        build_driver(settings) if settings.neo4j_password.get_secret_value() else None
    )
    app.state.graph_reader = (
        None
        if app.state.graph_driver is None
        else GraphReader(app.state.graph_driver, settings.neo4j_database)
    )

    app.state.token_service = TokenService.from_settings(settings)

    # Redis (or, without CONTEXTLEDGER_REDIS_URL, a per-process in-memory store).
    app.state.store = build_store(settings)
    app.state.rate_limiter = RateLimiter(app.state.store)
    app.state.retrieval_cache = RetrievalCache(
        app.state.store, ttl_seconds=settings.retrieval_cache_ttl_seconds
    )

    install_error_handlers(app)
    # Added first = innermost: correlation ids and access logs wrap idempotent replays too.
    app.add_middleware(
        IdempotencyMiddleware,
        store=app.state.store,
        tokens=app.state.token_service,
        allow_header_auth=settings.auth_mode == "development-headers",
        ttl_seconds=settings.idempotency_ttl_seconds,
        lock_seconds=settings.idempotency_lock_seconds,
    )
    app.add_middleware(CorrelationIdMiddleware, header_name=settings.correlation_id_header)
    app.include_router(api_router, prefix=API_V1_PREFIX)
    return app
