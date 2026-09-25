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

import ssl

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def ssl_context(settings: Settings) -> ssl.SSLContext | bool:
    """asyncpg ``ssl`` argument for ``db_ssl_mode``.

    ``require``: encrypted, certificate not checked (protects against passive sniffing
    only). ``verify-full``: the server certificate must chain to ``db_ssl_root_cert``
    and match the host name, which also defeats an active man in the middle.
    """
    if settings.db_ssl_mode == "disable":
        return False
    if settings.db_ssl_mode == "require":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    context = ssl.create_default_context(cafile=str(settings.db_ssl_root_cert))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


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
            "ssl": ssl_context(settings),
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
