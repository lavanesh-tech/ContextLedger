"""Recording temporal fact versions through FactService against real PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
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
from app.domain.facts import PrivacyScope, SourceType
from app.domain.roles import MembershipRole
from app.models.entity import Entity
from app.models.fact import Fact
from app.services.facts import FactService, RecordFactVersion
from tests.integration.factories import (
    add_member,
    admin_workspace,
    context,
    make_source,
    make_user,
    record,
    unique,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]

T1030 = datetime(2026, 9, 23, 10, 30, tzinfo=UTC)
T1415 = datetime(2026, 9, 23, 14, 15, tzinfo=UTC)
HOUR = timedelta(hours=1)


def credit_limit(customer: str, value: object, source_id: UUID, **kwargs: Any) -> RecordFactVersion:
    return RecordFactVersion(
        entity_type="customer",
        external_id=customer,
        property="credit_limit",
        value=value,
        source_id=source_id,
        **kwargs,
    )


async def test_first_version_creates_entity_fact_and_version_1(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")

    v1 = await record(sessions, ctx, credit_limit(customer, 2000, source.id, valid_from=T1030))

    assert v1.version == 1
    assert v1.value == 2000
    assert v1.supersedes_id is None
    assert v1.valid_from == T1030
    assert v1.valid_until is None
    assert v1.valid_until_recorded_at is None
    assert v1.observed_at == T1030  # defaults to valid_from
    assert v1.authority == source.default_authority
    assert v1.confidence == Decimal("1.000")
    assert v1.privacy_scope is PrivacyScope.INTERNAL
    assert v1.recorded_at.tzinfo is not None


async def test_credit_limit_example_supersedes_and_closes_the_previous_version(
    sessions: Sessions,
) -> None:
    """The spec's example: 2000 from 10:30 (v1), then 5000 from 14:15 (v2)."""
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")

    v1 = await record(sessions, ctx, credit_limit(customer, 2000, source.id, valid_from=T1030))
    v2 = await record(sessions, ctx, credit_limit(customer, 5000, source.id, valid_from=T1415))

    async with sessions() as session:
        history = await FactService(session).list_versions(ctx, v2.fact_id)

    assert v2.fact_id == v1.fact_id
    assert [(v.version, v.value) for v in history] == [(1, 2000), (2, 5000)]
    first, second = history
    assert first.valid_until == T1415  # closed exactly where v2 begins
    assert first.valid_until_recorded_at is not None
    assert first.valid_until_recorded_at == second.recorded_at  # same transaction
    assert second.supersedes_id == first.id
    assert second.valid_until is None


async def test_versions_never_overwrite_earlier_values(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")
    v1 = await record(sessions, ctx, credit_limit(customer, 2000, source.id, valid_from=T1030))
    await record(sessions, ctx, credit_limit(customer, 5000, source.id, valid_from=T1415))

    async with sessions() as session:
        reloaded = await FactService(session).get_version(ctx, v1.id)

    assert reloaded.value == 2000
    assert reloaded.valid_from == T1030
    assert reloaded.recorded_at == v1.recorded_at


@pytest.mark.parametrize("start", [T1030, T1030 - HOUR])
async def test_backdating_before_the_latest_version_is_rejected(
    sessions: Sessions, start: datetime
) -> None:
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")
    await record(sessions, ctx, credit_limit(customer, 2000, source.id, valid_from=T1030))

    with pytest.raises(ConflictError, match="revocation"):
        await record(sessions, ctx, credit_limit(customer, 1, source.id, valid_from=start))


async def test_fixed_windows_allow_gaps_but_not_overlaps(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    customer = unique("promo")
    await record(
        sessions,
        ctx,
        credit_limit(customer, 100, source.id, valid_from=T1030, valid_until=T1030 + 2 * HOUR),
    )

    with pytest.raises(ConflictError, match="overlaps"):
        await record(sessions, ctx, credit_limit(customer, 200, source.id, valid_from=T1030 + HOUR))

    v2 = await record(
        sessions, ctx, credit_limit(customer, 300, source.id, valid_from=T1030 + 3 * HOUR)
    )
    assert v2.version == 2


async def test_a_version_created_with_a_fixed_end_records_when_that_end_was_learned(
    sessions: Sessions,
) -> None:
    ctx, source = await admin_workspace(sessions)

    version = await record(
        sessions,
        ctx,
        credit_limit(unique("c"), 1, source.id, valid_from=T1030, valid_until=T1415),
    )

    assert version.valid_until == T1415
    assert version.valid_until_recorded_at == version.recorded_at


async def test_overrides_for_authority_confidence_privacy_and_observed_at(
    sessions: Sessions,
) -> None:
    ctx, source = await admin_workspace(sessions)
    observed = T1030 - timedelta(minutes=5)

    version = await record(
        sessions,
        ctx,
        credit_limit(
            unique("c"),
            {"amount": 2000, "currency": "USD"},
            source.id,
            valid_from=T1030,
            observed_at=observed,
            authority=40,
            confidence=0.8,
            privacy_scope=PrivacyScope.CONFIDENTIAL,
        ),
    )

    assert version.value == {"amount": 2000, "currency": "USD"}
    assert version.observed_at == observed
    assert version.authority == 40
    assert version.confidence == Decimal("0.800")
    assert version.privacy_scope is PrivacyScope.CONFIDENTIAL


async def test_invalid_input_is_rejected_before_any_write(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)

    with pytest.raises(ValidationFailedError):
        await record(
            sessions,
            ctx,
            credit_limit("c-1", 1, source.id, valid_from=datetime(2026, 9, 23, 10, 30)),  # noqa: DTZ001
        )


async def test_identifiers_are_normalised(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")

    await record(
        sessions,
        ctx,
        RecordFactVersion(
            entity_type=" Customer ",
            external_id=f" {customer} ",
            property="Billing.Credit_Limit",
            value=1,
            source_id=source.id,
            valid_from=T1030,
        ),
    )

    async with sessions() as session:
        entity = (await session.scalars(select(Entity).where(Entity.external_id == customer))).one()
        fact = (await session.scalars(select(Fact).where(Fact.entity_id == entity.id))).one()
    assert (entity.entity_type, fact.property) == ("customer", "billing.credit_limit")


async def test_viewers_cannot_record_but_engineers_can(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    viewer = await make_user(sessions)
    engineer = await make_user(sessions)
    await add_member(sessions, ctx, viewer, MembershipRole.VIEWER)
    await add_member(sessions, ctx, engineer, MembershipRole.ENGINEER)
    viewer_ctx = await context(sessions, ctx.organization_id, viewer.id)
    engineer_ctx = await context(sessions, ctx.organization_id, engineer.id)

    with pytest.raises(PermissionDeniedError):
        await record(
            sessions, viewer_ctx, credit_limit(unique("c"), 1, source.id, valid_from=T1030)
        )

    version = await record(
        sessions, engineer_ctx, credit_limit(unique("c"), 1, source.id, valid_from=T1030)
    )
    assert version.version == 1


async def test_duplicate_source_names_are_a_conflict(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)

    async with sessions() as session:
        with pytest.raises(ConflictError):
            await FactService(session).register_source(
                ctx, name=source.name, source_type=SourceType.API
            )


async def test_unknown_source_is_not_found(sessions: Sessions) -> None:
    ctx, _ = await admin_workspace(sessions)

    with pytest.raises(NotFoundError, match="source"):
        await record(sessions, ctx, credit_limit(unique("c"), 1, uuid4(), valid_from=T1030))


# --- tenant isolation -------------------------------------------------------------------


async def test_cannot_record_with_another_tenants_source(sessions: Sessions) -> None:
    ctx_a, _ = await admin_workspace(sessions)
    _, source_b = await admin_workspace(sessions)

    with pytest.raises(NotFoundError, match="source"):
        await record(sessions, ctx_a, credit_limit(unique("c"), 1, source_b.id, valid_from=T1030))


async def test_same_external_id_in_two_tenants_are_separate_facts(sessions: Sessions) -> None:
    ctx_a, source_a = await admin_workspace(sessions)
    ctx_b, source_b = await admin_workspace(sessions)

    in_a = await record(
        sessions, ctx_a, credit_limit("customer-991", 2000, source_a.id, valid_from=T1030)
    )
    in_b = await record(
        sessions, ctx_b, credit_limit("customer-991", 9999, source_b.id, valid_from=T1030)
    )

    assert in_a.fact_id != in_b.fact_id
    assert (in_a.version, in_b.version) == (1, 1)


async def test_cannot_read_another_tenants_versions_or_history(sessions: Sessions) -> None:
    ctx_a, _ = await admin_workspace(sessions)
    ctx_b, source_b = await admin_workspace(sessions)
    foreign = await record(
        sessions, ctx_b, credit_limit(unique("c"), 1, source_b.id, valid_from=T1030)
    )

    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await FactService(session).get_version(ctx_a, foreign.id)
    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await FactService(session).list_versions(ctx_a, foreign.fact_id)


# --- concurrency ---------------------------------------------------------------------


async def test_concurrent_writes_to_one_fact_form_a_single_valid_chain(sessions: Sessions) -> None:
    """Ten writers race on one fact. Whatever order they commit in, the result must be
    versions 1..n with no gaps, each superseding the previous, and no overlap."""
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")
    starts = [T1030 + i * timedelta(minutes=10) for i in range(10)]

    results = await asyncio.gather(
        *(
            record(sessions, ctx, credit_limit(customer, i, source.id, valid_from=start))
            for i, start in enumerate(starts)
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert successes, "at least the first committed writer must succeed"
    assert all(isinstance(f, ConflictError) for f in failures)  # out-of-order arrivals

    async with sessions() as session:
        history = await FactService(session).list_versions(ctx, successes[0].fact_id)
    assert [v.version for v in history] == list(range(1, len(successes) + 1))
    for previous, current in pairwise(history):
        assert current.supersedes_id == previous.id
        assert previous.valid_until == current.valid_from
        assert previous.valid_from < current.valid_from
    assert history[-1].valid_until is None


async def test_concurrent_first_writes_create_exactly_one_entity_and_fact(
    sessions: Sessions,
) -> None:
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")

    await asyncio.gather(
        *(
            record(sessions, ctx, credit_limit(customer, i, source.id, valid_from=T1030 + i * HOUR))
            for i in range(5)
        ),
        return_exceptions=True,
    )

    async with sessions() as session:
        entities = await session.scalar(
            select(func.count()).select_from(Entity).where(Entity.external_id == customer)
        )
        facts = await session.scalar(
            select(func.count())
            .select_from(Fact)
            .join(Entity, Entity.id == Fact.entity_id)
            .where(Entity.external_id == customer)
        )
    assert (entities, facts) == (1, 1)


async def test_another_source_of_same_org_can_supersede(sessions: Sessions) -> None:
    ctx, billing = await admin_workspace(sessions)
    crm = await make_source(sessions, ctx, source_type=SourceType.API, default_authority=60)
    customer = unique("customer")

    await record(sessions, ctx, credit_limit(customer, 2000, billing.id, valid_from=T1030))
    v2 = await record(sessions, ctx, credit_limit(customer, 2500, crm.id, valid_from=T1415))

    assert (v2.version, v2.source_id, v2.authority) == (2, crm.id, 60)
