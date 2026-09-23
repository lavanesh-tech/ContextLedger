"""The database itself rejects invalid data, whatever code path writes it."""

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration.factories import make_org, make_user, unique

pytestmark = pytest.mark.integration


async def _execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)


async def test_duplicate_slug_is_rejected(engine: AsyncEngine) -> None:
    slug = unique("dup")
    await _execute(engine, "INSERT INTO organizations (slug, name) VALUES (:s, 'A')", s=slug)

    with pytest.raises(IntegrityError, match="uq_organizations_slug"):
        await _execute(engine, "INSERT INTO organizations (slug, name) VALUES (:s, 'B')", s=slug)


@pytest.mark.parametrize("slug", ["Bad Slug", "-leading", "x"])
async def test_malformed_slug_is_rejected(engine: AsyncEngine, slug: str) -> None:
    with pytest.raises(IntegrityError, match="ck_organizations_slug_format"):
        await _execute(engine, "INSERT INTO organizations (slug, name) VALUES (:s, 'A')", s=slug)


async def test_upper_case_email_is_rejected(engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError, match="ck_users_email_lowercase"):
        await _execute(
            engine,
            "INSERT INTO users (email, display_name) VALUES (:e, 'X')",
            e=f"{unique('Upper')}@Example.com",
        )


async def test_unknown_role_is_rejected(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    admin = await make_user(sessions)
    other = await make_user(sessions)
    org = await make_org(sessions, admin)

    with pytest.raises(IntegrityError, match="ck_organization_memberships_role_valid"):
        await _execute(
            engine,
            "INSERT INTO organization_memberships (organization_id, user_id, role) "
            "VALUES (:o, :u, 'OWNER')",
            o=org.id,
            u=other.id,
        )


async def test_duplicate_membership_is_rejected(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    admin = await make_user(sessions)
    org = await make_org(sessions, admin)

    with pytest.raises(IntegrityError, match="uq_organization_memberships_organization_id_user_id"):
        await _execute(
            engine,
            "INSERT INTO organization_memberships (organization_id, user_id, role) "
            "VALUES (:o, :u, 'VIEWER')",
            o=org.id,
            u=admin.id,
        )


async def test_membership_must_reference_an_existing_organization(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    user = await make_user(sessions)

    with pytest.raises(IntegrityError, match="fk_organization_memberships_organization_id"):
        await _execute(
            engine,
            "INSERT INTO organization_memberships (organization_id, user_id, role) "
            "VALUES (gen_random_uuid(), :u, 'VIEWER')",
            u=user.id,
        )


async def test_organization_with_members_cannot_be_deleted(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    admin = await make_user(sessions)
    org = await make_org(sessions, admin)

    with pytest.raises(IntegrityError, match="fk_organization_memberships_organization_id"):
        await _execute(engine, "DELETE FROM organizations WHERE id = :o", o=org.id)
