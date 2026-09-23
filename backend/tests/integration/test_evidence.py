"""Evidence capture, linking and provenance through the services (real PostgreSQL)."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.domain.evidence import EvidenceRelation, EvidenceType, content_hash
from app.domain.facts import SourceType
from app.domain.roles import MembershipRole
from app.models.fact import FactVersion
from app.services.evidence import EvidenceService
from app.services.facts import FactService, RecordFactVersion
from tests.integration.factories import (
    add_member,
    admin_workspace,
    capture,
    context,
    make_source,
    make_user,
    record,
    unique,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)


def credit_limit(customer: str, value: int, source_id: UUID, **kwargs: Any) -> RecordFactVersion:
    return RecordFactVersion(
        entity_type="customer",
        external_id=customer,
        property="credit_limit",
        value=value,
        source_id=source_id,
        valid_from=T0,
        **kwargs,
    )


async def test_capture_stores_normalised_content_and_its_hash(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)

    captured = await capture(sessions, ctx, source, excerpt="  Credit limit: 2000\r\n")

    evidence = captured.evidence
    assert captured.created is True
    assert evidence.excerpt == "Credit limit: 2000"
    assert evidence.content_sha256 == content_hash("Credit limit: 2000")
    assert evidence.metadata_ == {"page": 3}
    assert evidence.uri == "s3://contracts/customer-991.pdf"
    assert evidence.recorded_at.tzinfo is not None


async def test_capturing_the_same_content_twice_is_idempotent(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)

    first = await capture(sessions, ctx, source, excerpt="limit 2000")
    second = await capture(sessions, ctx, source, excerpt="limit 2000\n")

    assert second.created is False
    assert second.evidence.id == first.evidence.id


async def test_concurrent_identical_captures_create_one_row(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    text = unique("race")

    results = await asyncio.gather(
        *(capture(sessions, ctx, source, excerpt=text) for _ in range(5))
    )

    assert len({r.evidence.id for r in results}) == 1
    assert sum(r.created for r in results) == 1


async def test_same_content_from_another_source_is_separate_evidence(sessions: Sessions) -> None:
    ctx, billing = await admin_workspace(sessions)
    crm = await make_source(sessions, ctx, source_type=SourceType.API)

    from_billing = await capture(sessions, ctx, billing, excerpt="limit 2000")
    from_crm = await capture(sessions, ctx, crm, excerpt="limit 2000")

    assert from_billing.evidence.id != from_crm.evidence.id


async def test_evidence_linked_while_recording_appears_in_provenance(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    contract = await capture(sessions, ctx, source, excerpt="Contract: limit 2000")
    email = await capture(sessions, ctx, source, excerpt="Email: approved 2000")

    version = await record(
        sessions,
        ctx,
        credit_limit(
            unique("c"), 2000, source.id, evidence_ids=(contract.evidence.id, email.evidence.id)
        ),
    )
    async with sessions() as session:
        provenance = await EvidenceService(session).provenance(ctx, version.id)

    assert provenance.version.id == version.id
    assert provenance.source.id == source.id
    assert {e.evidence.id for e in provenance.evidence} == {
        contract.evidence.id,
        email.evidence.id,
    }
    assert all(e.relation is EvidenceRelation.SUPPORTS for e in provenance.evidence)
    assert all(e.linked_by_user_id == ctx.user_id for e in provenance.evidence)


async def test_unknown_evidence_rolls_back_the_whole_version(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    customer = unique("c")

    with pytest.raises(NotFoundError, match="evidence"):
        await record(
            sessions, ctx, credit_limit(customer, 2000, source.id, evidence_ids=(uuid4(),))
        )

    async with sessions() as session:
        versions = await session.scalar(
            select(func.count())
            .select_from(FactVersion)
            .where(FactVersion.organization_id == ctx.organization_id)
        )
    assert versions == 0


async def test_attach_later_is_idempotent(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    version = await record(sessions, ctx, credit_limit(unique("c"), 2000, source.id))
    evidence = await capture(sessions, ctx, source)

    async with sessions() as session:
        first = await EvidenceService(session).attach(
            ctx, fact_version_id=version.id, evidence_id=evidence.evidence.id
        )
    async with sessions() as session:
        again = await EvidenceService(session).attach(
            ctx, fact_version_id=version.id, evidence_id=evidence.evidence.id
        )

    assert (first, again) == (True, False)


async def test_a_link_cannot_change_its_meaning(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    version = await record(sessions, ctx, credit_limit(unique("c"), 2000, source.id))
    evidence = await capture(sessions, ctx, source)
    async with sessions() as session:
        await EvidenceService(session).attach(
            ctx, fact_version_id=version.id, evidence_id=evidence.evidence.id
        )

    async with sessions() as session:
        with pytest.raises(ConflictError):
            await EvidenceService(session).attach(
                ctx,
                fact_version_id=version.id,
                evidence_id=evidence.evidence.id,
                relation=EvidenceRelation.CONTRADICTS,
            )


async def test_viewers_can_read_provenance_but_not_capture(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    version = await record(sessions, ctx, credit_limit(unique("c"), 2000, source.id))
    viewer = await make_user(sessions)
    await add_member(sessions, ctx, viewer, MembershipRole.VIEWER)
    viewer_ctx = await context(sessions, ctx.organization_id, viewer.id)

    with pytest.raises(PermissionDeniedError):
        await capture(sessions, viewer_ctx, source)
    async with sessions() as session:
        provenance = await EvidenceService(session).provenance(viewer_ctx, version.id)
    assert provenance.evidence == ()


async def test_sources_are_listed_by_name(sessions: Sessions) -> None:
    ctx, first = await admin_workspace(sessions)
    second = await make_source(sessions, ctx, source_type=SourceType.HUMAN)

    async with sessions() as session:
        sources = await EvidenceService(session).list_sources(ctx)
    async with sessions() as session:
        fetched = await EvidenceService(session).get_source(ctx, second.id)

    assert [s.id for s in sources] == [s.id for s in sorted([first, second], key=lambda s: s.name)]
    assert fetched.source_type is SourceType.HUMAN


async def test_invalid_capture_input_is_rejected(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)

    async with sessions() as session:
        with pytest.raises(ValidationFailedError):
            await EvidenceService(session).capture_evidence(
                ctx,
                source_id=source.id,
                evidence_type=EvidenceType.API_RESPONSE,
                excerpt="ok",
                captured_at=datetime(2026, 1, 15, 10, 30),  # noqa: DTZ001
            )


# --- tenant isolation --------------------------------------------------------------------


async def test_cannot_capture_with_another_tenants_source(sessions: Sessions) -> None:
    ctx_a, _ = await admin_workspace(sessions)
    _, source_b = await admin_workspace(sessions)

    with pytest.raises(NotFoundError, match="source"):
        await capture(sessions, ctx_a, source_b)


async def test_cannot_link_another_tenants_evidence(sessions: Sessions) -> None:
    ctx_a, source_a = await admin_workspace(sessions)
    ctx_b, source_b = await admin_workspace(sessions)
    foreign = await capture(sessions, ctx_b, source_b)

    with pytest.raises(NotFoundError, match="evidence"):
        await record(
            sessions,
            ctx_a,
            credit_limit(unique("c"), 1, source_a.id, evidence_ids=(foreign.evidence.id,)),
        )


async def test_cannot_read_another_tenants_provenance(sessions: Sessions) -> None:
    ctx_a, _ = await admin_workspace(sessions)
    ctx_b, source_b = await admin_workspace(sessions)
    foreign = await record(sessions, ctx_b, credit_limit(unique("c"), 1, source_b.id))

    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await EvidenceService(session).provenance(ctx_a, foreign.id)
    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await EvidenceService(session).attach(
                ctx_a, fact_version_id=foreign.id, evidence_id=uuid4()
            )


async def test_fact_service_still_records_without_evidence(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)

    async with sessions() as session:
        version = await FactService(session).record_version(
            ctx, credit_limit(unique("c"), 2000, source.id)
        )

    assert version.version == 1
