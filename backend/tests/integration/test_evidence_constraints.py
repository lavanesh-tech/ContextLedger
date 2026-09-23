"""The database protects provenance even from code that bypasses the services."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.services.facts import RecordFactVersion
from tests.integration.factories import admin_workspace, capture, record, unique

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)


async def _execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)


async def _linked(sessions: Sessions) -> tuple[Any, Any, Any]:
    ctx, source = await admin_workspace(sessions)
    evidence = await capture(sessions, ctx, source, excerpt=unique("excerpt"))
    version = await record(
        sessions,
        ctx,
        RecordFactVersion(
            entity_type="customer",
            external_id=unique("c"),
            property="credit_limit",
            value=2000,
            source_id=source.id,
            valid_from=T0,
            evidence_ids=(evidence.evidence.id,),
        ),
    )
    return ctx, evidence.evidence, version


async def test_evidence_cannot_be_edited(engine: AsyncEngine, sessions: Sessions) -> None:
    _, evidence, _ = await _linked(sessions)

    with pytest.raises(IntegrityError, match="append-only"):
        await _execute(
            engine, "UPDATE evidence SET excerpt = 'forged' WHERE id = :id", id=evidence.id
        )


async def test_evidence_cannot_be_deleted(engine: AsyncEngine, sessions: Sessions) -> None:
    _, evidence, _ = await _linked(sessions)

    with pytest.raises(IntegrityError, match="append-only"):
        await _execute(engine, "DELETE FROM evidence WHERE id = :id", id=evidence.id)


async def test_links_cannot_be_removed_or_rewritten(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    _, evidence, version = await _linked(sessions)

    with pytest.raises(IntegrityError, match="append-only"):
        await _execute(
            engine,
            "DELETE FROM fact_version_evidence WHERE evidence_id = :e AND fact_version_id = :v",
            e=evidence.id,
            v=version.id,
        )
    with pytest.raises(IntegrityError, match="append-only"):
        await _execute(
            engine,
            "UPDATE fact_version_evidence SET relation = 'CONTRADICTS' WHERE evidence_id = :e",
            e=evidence.id,
        )


async def test_a_link_cannot_cross_tenants(engine: AsyncEngine, sessions: Sessions) -> None:
    ctx_a, _, version_a = await _linked(sessions)
    _, evidence_b, _ = await _linked(sessions)

    with pytest.raises(IntegrityError, match="fk_fact_version_evidence_organization_id_evidence"):
        await _execute(
            engine,
            "INSERT INTO fact_version_evidence (organization_id, fact_version_id, evidence_id, "
            "relation) VALUES (:org, :v, :e, 'SUPPORTS')",
            org=ctx_a.organization_id,
            v=version_a.id,
            e=evidence_b.id,
        )


async def test_malformed_hash_is_rejected(engine: AsyncEngine, sessions: Sessions) -> None:
    ctx, evidence, _ = await _linked(sessions)

    with pytest.raises(IntegrityError, match="ck_evidence_content_sha256_format"):
        await _execute(
            engine,
            "INSERT INTO evidence (organization_id, source_id, evidence_type, excerpt, "
            "content_sha256, metadata, privacy_scope, captured_at) "
            "VALUES (:org, :src, 'API_RESPONSE', 'x', 'NOT-A-HASH', '{}', 'INTERNAL', now())",
            org=ctx.organization_id,
            src=evidence.source_id,
        )


async def test_metadata_must_be_an_object(engine: AsyncEngine, sessions: Sessions) -> None:
    ctx, evidence, _ = await _linked(sessions)

    with pytest.raises(IntegrityError, match="ck_evidence_metadata_is_object"):
        await _execute(
            engine,
            "INSERT INTO evidence (organization_id, source_id, evidence_type, excerpt, "
            "content_sha256, metadata, privacy_scope, captured_at) "
            "VALUES (:org, :src, 'API_RESPONSE', 'x', repeat('a', 64), '[1]', 'INTERNAL', now())",
            org=ctx.organization_id,
            src=evidence.source_id,
        )
