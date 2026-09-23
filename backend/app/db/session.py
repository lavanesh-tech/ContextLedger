"""Async engine and session factory.

Lifecycle:

* ``create_app()`` builds one engine (and its connection pool) per process.
  Creating an engine does not open a connection, so the API starts even when
  PostgreSQL is down; the readiness check reports that instead.
* Each request that needs the database gets its own ``AsyncSession`` from the
  session factory (see ``app.api.dependencies.SessionDep``).
* The application lifespan disposes the engine on shutdown, closing pooled
  connections cleanly.

Transaction boundaries belong to services (``async with session.begin():``),
not to routers or repositories.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        # Detect connections killed by a DB restart/failover before using them.
        pool_pre_ping=True,
        echo=settings.db_echo,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            "server_settings": {
                "application_name": settings.db_application_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: after commit, attribute access must not trigger
    # implicit (and in async code, illegal) lazy database I/O.
    return async_sessionmaker(engine, expire_on_commit=False)
