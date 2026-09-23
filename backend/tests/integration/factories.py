"""Test-data builders that go through the real services (so rules are exercised)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.evidence import EvidenceType
from app.domain.facts import SourceType
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.models.fact import FactVersion
from app.models.organization import Organization
from app.models.source import FactSource
from app.models.user import User
from app.services.evidence import CapturedEvidence, EvidenceService
from app.services.facts import FactService, RecordFactVersion
from app.services.memberships import MembershipService
from app.services.organizations import OrganizationService
from app.services.tenancy import TenancyService
from app.services.users import UserService

Sessions = async_sessionmaker[AsyncSession]


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


async def make_user(sessions: Sessions, *, active: bool = True) -> User:
    async with sessions() as session:
        user = await UserService(session).register(
            email=f"{unique('user')}@example.com", display_name="Test User"
        )
    if not active:
        async with sessions() as session, session.begin():
            await session.execute(update(User).where(User.id == user.id).values(is_active=False))
    return user


async def make_org(sessions: Sessions, creator: User) -> Organization:
    async with sessions() as session:
        return await OrganizationService(session).create(
            name="Test Org", slug=unique("org"), creator_user_id=creator.id
        )


async def context(sessions: Sessions, organization_id: UUID, user_id: UUID) -> TenantContext:
    async with sessions() as session:
        return await TenancyService(session).resolve(
            organization_id=organization_id, user_id=user_id
        )


async def add_member(
    sessions: Sessions, actor: TenantContext, user: User, role: MembershipRole
) -> None:
    async with sessions() as session:
        await MembershipService(session).add_member(actor, user_id=user.id, role=role)


async def role_of(sessions: Sessions, organization_id: UUID, user_id: UUID) -> MembershipRole:
    return (await context(sessions, organization_id, user_id)).role


async def make_source(
    sessions: Sessions,
    ctx: TenantContext,
    *,
    source_type: SourceType = SourceType.SYSTEM_OF_RECORD,
    default_authority: int = 90,
) -> FactSource:
    async with sessions() as session:
        return await FactService(session).register_source(
            ctx,
            name=unique("source"),
            source_type=source_type,
            uri="https://billing.example.com",
            default_authority=default_authority,
        )


async def record(sessions: Sessions, ctx: TenantContext, command: RecordFactVersion) -> FactVersion:
    async with sessions() as session:
        return await FactService(session).record_version(ctx, command)


async def admin_workspace(sessions: Sessions) -> tuple[TenantContext, FactSource]:
    """A fresh organization, its ADMIN's context, and one trusted source."""
    admin = await make_user(sessions)
    org = await make_org(sessions, admin)
    ctx = await context(sessions, org.id, admin.id)
    return ctx, await make_source(sessions, ctx)


async def capture(
    sessions: Sessions,
    ctx: TenantContext,
    source: FactSource,
    excerpt: str = "Credit limit for customer-991 is 2000 USD.",
) -> CapturedEvidence:
    async with sessions() as session:
        return await EvidenceService(session).capture_evidence(
            ctx,
            source_id=source.id,
            evidence_type=EvidenceType.DOCUMENT_EXCERPT,
            excerpt=excerpt,
            captured_at=datetime(2026, 1, 15, 10, 29, tzinfo=UTC),
            uri="s3://contracts/customer-991.pdf",
            metadata={"page": 3},
        )
