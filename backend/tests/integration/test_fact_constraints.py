"""PostgreSQL itself protects temporal integrity, even from code that bypasses services."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.services.facts import RecordFactVersion
from tests.integration.factories import admin_workspace, record, unique

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 9, 23, 10, 30, tzinfo=UTC)


async def _execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)


async def _two_versions(sessions: Sessions) -> tuple[UUID, UUID, UUID]:
    """Returns (organization_id, fact_id, id of closed version 1)."""
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")

    def command(value: int, valid_from: datetime) -> RecordFactVersion:
        return RecordFactVersion(
            entity_type="customer",
            external_id=customer,
            property="credit_limit",
            value=value,
            source_id=source.id,
            valid_from=valid_from,
        )

    v1 = await record(sessions, ctx, command(2000, T0))
    await record(sessions, ctx, command(5000, T0 + timedelta(hours=4)))
    return ctx.organization_id, v1.fact_id, v1.id


async def test_versions_cannot_be_edited(engine: AsyncEngine, sessions: Sessions) -> None:
    _, _, v1 = await _two_versions(sessions)

    with pytest.raises(IntegrityError, match="append-only"):
        await _execute(engine, "UPDATE fact_versions SET value = '1'::jsonb WHERE id = :id", id=v1)


async def test_versions_cannot_be_deleted(engine: AsyncEngine, sessions: Sessions) -> None:
    _, _, v1 = await _two_versions(sessions)

    with pytest.raises(IntegrityError, match="append-only"):
        await _execute(engine, "DELETE FROM fact_versions WHERE id = :id", id=v1)


async def test_a_closed_version_cannot_be_reopened_or_re_closed(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    _, _, v1 = await _two_versions(sessions)

    with pytest.raises(IntegrityError, match="only be set once"):
        await _execute(
            engine,
            "UPDATE fact_versions SET valid_until = NULL, valid_until_recorded_at = NULL "
            "WHERE id = :id",
            id=v1,
        )


async def test_overlapping_versions_are_rejected_by_the_exclusion_constraint(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    org, fact, v1 = await _two_versions(sessions)

    # Hand-craft a version 3 that overlaps version 2 (open-ended since T0+4h).
    with pytest.raises(IntegrityError, match="ex_fact_versions_no_overlapping_validity"):
        await _execute(
            engine,
            """
            INSERT INTO fact_versions (organization_id, fact_id, version, value, source_id,
                valid_from, observed_at, supersedes_id, authority, confidence, privacy_scope)
            SELECT organization_id, fact_id, 3, '7'::jsonb, source_id,
                   :start, :start,
                   (SELECT id FROM fact_versions WHERE fact_id = :f AND version = 2),
                   50, 1, 'INTERNAL'
            FROM fact_versions WHERE id = :v1
            """,
            start=T0 + timedelta(hours=5),
            f=fact,
            v1=v1,
        )
    assert org is not None


async def test_a_version_can_only_supersede_a_version_of_the_same_fact(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    ctx, source = await admin_workspace(sessions)
    other = await record(
        sessions,
        ctx,
        RecordFactVersion(
            entity_type="customer",
            external_id=unique("other"),
            property="credit_limit",
            value=1,
            source_id=source.id,
            valid_from=T0,
        ),
    )
    _, fact, _ = await _two_versions(sessions)

    # Window placed before version 1, so only the lineage FK can fail.
    with pytest.raises(IntegrityError, match="fk_fact_versions_fact_id_fact_versions"):
        await _execute(
            engine,
            """
            INSERT INTO fact_versions (organization_id, fact_id, version, value, source_id,
                valid_from, valid_until, valid_until_recorded_at, observed_at, supersedes_id,
                authority, confidence, privacy_scope)
            SELECT organization_id, fact_id, 3, '7'::jsonb, source_id,
                   :start, :end, now(), :start, :foreign, 50, 1, 'INTERNAL'
            FROM fact_versions WHERE fact_id = :f AND version = 1
            """,
            start=T0 - timedelta(days=10),
            end=T0 - timedelta(days=9),
            foreign=other.id,
            f=fact,
        )


async def test_a_fact_cannot_reference_another_tenants_entity(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    org_a, _, _ = await _two_versions(sessions)
    _, fact_b, _ = await _two_versions(sessions)

    with pytest.raises(IntegrityError, match="fk_facts_organization_id_entities"):
        await _execute(
            engine,
            "INSERT INTO facts (organization_id, entity_id, property) "
            "SELECT :org_a, entity_id, 'stolen' FROM facts WHERE id = :fact_b",
            org_a=org_a,
            fact_b=fact_b,
        )


async def test_null_json_values_are_rejected(engine: AsyncEngine, sessions: Sessions) -> None:
    _, fact, _ = await _two_versions(sessions)

    with pytest.raises(IntegrityError, match="ck_fact_versions_value_not_null"):
        await _execute(
            engine,
            """
            INSERT INTO fact_versions (organization_id, fact_id, version, value, source_id,
                valid_from, observed_at, supersedes_id, authority, confidence, privacy_scope)
            SELECT organization_id, fact_id, 3, 'null'::jsonb, source_id, :start, :start,
                   (SELECT id FROM fact_versions WHERE fact_id = :f AND version = 2),
                   50, 1, 'INTERNAL'
            FROM fact_versions WHERE fact_id = :f AND version = 1
            """,
            start=T0 + timedelta(days=2),
            f=fact,
        )


async def test_version_numbers_are_unique_per_fact(engine: AsyncEngine, sessions: Sessions) -> None:
    _, fact, _ = await _two_versions(sessions)

    with pytest.raises(IntegrityError, match="uq_fact_versions_"):
        await _execute(
            engine,
            """
            INSERT INTO fact_versions (organization_id, fact_id, version, value, source_id,
                valid_from, valid_until, valid_until_recorded_at, observed_at, supersedes_id,
                authority, confidence, privacy_scope)
            SELECT organization_id, fact_id, 2, '1'::jsonb, source_id,
                   :start, :end, now(), :start, id, 50, 1, 'INTERNAL'
            FROM fact_versions WHERE fact_id = :f AND version = 1
            """,
            start=T0 - timedelta(days=10),
            end=T0 - timedelta(days=9),
            f=fact,
        )
