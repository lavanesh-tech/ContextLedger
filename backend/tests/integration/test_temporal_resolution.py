"""Temporal resolution against real PostgreSQL."""

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.services.facts import RecordFactVersion
from app.services.temporal import TemporalService
from app.temporal import reference
from app.temporal.model import VersionSnapshot
from tests.integration.factories import (
    add_member,
    admin_workspace,
    context,
    make_user,
    record,
    unique,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
US = timedelta(microseconds=1)


def at(hour: int, minute: int = 0) -> datetime:
    # A fixed date safely in the past: "current" questions resolve against the real
    # clock, so story data must never lie in the future relative to when tests run.
    return datetime(2026, 1, 15, hour, minute, tzinfo=UTC)


def command(
    customer: str, prop: str, value: object, source_id: UUID, **kwargs: Any
) -> RecordFactVersion:
    return RecordFactVersion(
        entity_type="customer",
        external_id=customer,
        property=prop,
        value=value,
        source_id=source_id,
        **kwargs,
    )


async def _credit_limit_story(
    sessions: Sessions,
) -> tuple[TenantContext, str, VersionSnapshot, VersionSnapshot]:
    """customer: credit_limit 2000 from 10:30, then 5000 from 14:15; status ACTIVE from 09:00."""
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")
    await record(sessions, ctx, command(customer, "status", "ACTIVE", source.id, valid_from=at(9)))
    await record(
        sessions, ctx, command(customer, "credit_limit", 2000, source.id, valid_from=at(10, 30))
    )
    await record(
        sessions, ctx, command(customer, "credit_limit", 5000, source.id, valid_from=at(14, 15))
    )
    async with sessions() as session:
        facts = await TemporalService(session).facts_at(
            ctx, entity_type="customer", external_id=customer
        )
    credit = next(f for f in facts if f.property == "credit_limit")
    async with sessions() as session:
        v1, v2 = await TemporalService(session).history(ctx, credit.fact_id)
    return ctx, customer, v1, v2


async def _facts(
    sessions: Sessions,
    ctx: TenantContext,
    customer: str,
    valid_at: datetime | None = None,
    known_at: datetime | None = None,
) -> dict[str, object]:
    async with sessions() as session:
        facts = await TemporalService(session).facts_at(
            ctx, entity_type="customer", external_id=customer, valid_at=valid_at, known_at=known_at
        )
    return {f.property: f.version.value for f in facts}


# --- the spec's questions ----------------------------------------------------------------


async def test_current_credit_limit_is_version_2(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)

    assert await _facts(sessions, ctx, customer) == {"credit_limit": 5000, "status": "ACTIVE"}


async def test_value_at_11_is_version_1(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)

    assert await _facts(sessions, ctx, customer, valid_at=at(11)) == {
        "credit_limit": 2000,
        "status": "ACTIVE",
    }


async def test_before_any_value_only_earlier_facts_resolve(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)

    assert await _facts(sessions, ctx, customer, valid_at=at(10)) == {"status": "ACTIVE"}
    assert await _facts(sessions, ctx, customer, valid_at=at(8)) == {}


async def test_knowledge_at_version_1s_recording_ignores_the_later_update(
    sessions: Sessions,
) -> None:
    """At the moment v1 was recorded, ContextLedger believed 2000 applied indefinitely."""
    ctx, customer, v1, v2 = await _credit_limit_story(sessions)

    believed = await _facts(sessions, ctx, customer, valid_at=at(15), known_at=v1.recorded_at)
    actual = await _facts(sessions, ctx, customer, valid_at=at(15), known_at=v2.recorded_at)

    assert believed["credit_limit"] == 2000
    assert actual["credit_limit"] == 5000


async def test_snapshot_as_known_hides_an_end_not_yet_learned(sessions: Sessions) -> None:
    ctx, customer, v1, _ = await _credit_limit_story(sessions)

    async with sessions() as session:
        facts = await TemporalService(session).facts_at(
            ctx,
            entity_type="customer",
            external_id=customer,
            valid_at=at(11),
            known_at=v1.recorded_at,
        )
    credit = next(f for f in facts if f.property == "credit_limit")

    assert credit.version.id == v1.id
    assert credit.version.valid_until is None  # the end at 14:15 was learned later


async def test_nothing_is_known_before_the_first_recording(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)

    assert await _facts(sessions, ctx, customer, valid_at=at(11), known_at=at(0)) == {}


async def test_transaction_time_is_strictly_increasing_per_fact(sessions: Sessions) -> None:
    _, _, v1, v2 = await _credit_limit_story(sessions)

    assert v1.recorded_at < v2.recorded_at
    assert v1.valid_until_recorded_at == v2.recorded_at


# --- history, lineage, timeline, changes ---------------------------------------------------


async def test_history_as_known(sessions: Sessions) -> None:
    ctx, _, v1, _ = await _credit_limit_story(sessions)

    async with sessions() as session:
        full = await TemporalService(session).history(ctx, v1.fact_id)
    async with sessions() as session:
        early = await TemporalService(session).history(ctx, v1.fact_id, known_at=v1.recorded_at)

    assert [v.version for v in full] == [1, 2]
    assert [(v.version, v.valid_until) for v in early] == [(1, None)]


async def test_lineage_links_versions_in_both_directions(sessions: Sessions) -> None:
    ctx, _, v1, v2 = await _credit_limit_story(sessions)

    async with sessions() as session:
        first = await TemporalService(session).lineage(ctx, v1.id)
    async with sessions() as session:
        second = await TemporalService(session).lineage(ctx, v2.id)

    assert first.ancestors == ()
    assert first.superseded_by is not None and first.superseded_by.id == v2.id
    assert [a.id for a in second.ancestors] == [v1.id]
    assert second.superseded_by is None
    assert second.version.supersedes_id == v1.id


async def test_entity_timeline_is_in_valid_time_order(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)

    async with sessions() as session:
        timeline = await TemporalService(session).entity_timeline(
            ctx, entity_type="customer", external_id=customer
        )

    assert [(e.property, e.version.value) for e in timeline] == [
        ("status", "ACTIVE"),
        ("credit_limit", 2000),
        ("credit_limit", 5000),
    ]


async def test_changes_between_reports_only_what_changed(sessions: Sessions) -> None:
    ctx, customer, v1, v2 = await _credit_limit_story(sessions)

    async with sessions() as session:
        changes = await TemporalService(session).changes_between(
            ctx, entity_type="customer", external_id=customer, start=at(11), end=at(15)
        )

    assert len(changes) == 1  # status did not change
    change = changes[0]
    assert change.property == "credit_limit"
    assert change.before is not None and change.before.id == v1.id
    assert change.after is not None and change.after.id == v2.id
    assert [t.id for t in change.transitions] == [v2.id]


async def test_changes_between_requires_a_forward_window(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)

    async with sessions() as session:
        with pytest.raises(ValidationFailedError):
            await TemporalService(session).changes_between(
                ctx, entity_type="customer", external_id=customer, start=at(15), end=at(11)
            )


# --- access control ------------------------------------------------------------------


async def test_viewers_can_read_temporal_answers(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)
    viewer = await make_user(sessions)
    await add_member(sessions, ctx, viewer, MembershipRole.VIEWER)
    viewer_ctx = await context(sessions, ctx.organization_id, viewer.id)

    assert (await _facts(sessions, viewer_ctx, customer))["credit_limit"] == 5000


async def test_other_tenants_cannot_resolve_the_entity(sessions: Sessions) -> None:
    _, customer, v1, _ = await _credit_limit_story(sessions)
    other_ctx, _ = await admin_workspace(sessions)

    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await TemporalService(session).facts_at(
                other_ctx, entity_type="customer", external_id=customer
            )
    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await TemporalService(session).lineage(other_ctx, v1.id)
    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await TemporalService(session).history(other_ctx, v1.fact_id)


async def test_forged_context_is_rejected(sessions: Sessions) -> None:
    ctx, customer, _, _ = await _credit_limit_story(sessions)
    outsider = await make_user(sessions)
    forged = TenantContext(
        organization_id=ctx.organization_id, user_id=outsider.id, role=MembershipRole.ADMIN
    )

    async with sessions() as session:
        with pytest.raises(PermissionDeniedError):
            await TemporalService(session).facts_at(
                forged, entity_type="customer", external_id=customer
            )


async def test_unknown_entity_is_not_found(sessions: Sessions) -> None:
    ctx, _ = await admin_workspace(sessions)

    async with sessions() as session:
        with pytest.raises(NotFoundError, match="entity"):
            await TemporalService(session).facts_at(
                ctx, entity_type="customer", external_id=str(uuid4())
            )


# --- differential test: SQL engine == reference semantics --------------------------------


def _random_history(rng: random.Random, start: datetime) -> list[tuple[datetime, datetime | None]]:
    """Valid windows obeying the supersession rules: increasing starts, gaps, fixed ends."""
    windows: list[tuple[datetime, datetime | None]] = []
    cursor = start
    for _ in range(rng.randint(1, 6)):
        valid_from = cursor + timedelta(minutes=rng.randint(0 if windows else 1, 90))
        if windows and valid_from <= windows[-1][0]:
            valid_from = windows[-1][0] + timedelta(minutes=1)
        valid_until = None
        if rng.random() < 0.3:
            valid_until = valid_from + timedelta(minutes=rng.randint(1, 60))
        windows.append((valid_from, valid_until))
        cursor = valid_until or valid_from
    return windows


async def test_sql_engine_matches_the_reference_semantics(sessions: Sessions) -> None:
    rng = random.Random(20260923)  # fixed seed: reproducible
    ctx, source = await admin_workspace(sessions)
    customer = unique("customer")
    properties = [f"p{i}" for i in range(6)]

    for prop in properties:
        for number, (valid_from, valid_until) in enumerate(_random_history(rng, at(8))):
            await record(
                sessions,
                ctx,
                command(
                    customer,
                    prop,
                    f"{prop}-v{number + 1}",
                    source.id,
                    valid_from=valid_from,
                    valid_until=valid_until,
                ),
            )

    async with sessions() as session:
        timeline = await TemporalService(session).entity_timeline(
            ctx, entity_type="customer", external_id=customer
        )
    history: dict[str, list[VersionSnapshot]] = {}
    for entry in timeline:
        history.setdefault(entry.property, []).append(entry.version)

    # Every boundary (and one microsecond either side) in both timelines, plus random points.
    valid_points: set[datetime] = set()
    known_points: set[datetime | None] = {None, at(0)}
    for versions in history.values():
        for v in versions:
            for t in filter(None, (v.valid_from, v.valid_until)):
                valid_points.update({t - US, t, t + US})
            for k in filter(None, (v.recorded_at, v.valid_until_recorded_at)):
                known_points.update({k - US, k, k + US})
    valid_points.update(at(8) + timedelta(seconds=rng.randint(0, 12 * 3600)) for _ in range(20))

    pairs = [(t, k) for t in sorted(valid_points) for k in known_points]
    sample = rng.sample(pairs, k=min(250, len(pairs)))

    mismatches = []
    for valid_at, known_at in sample:
        expected = {
            prop: resolved.id
            for prop, versions in history.items()
            if (resolved := reference.resolve(versions, valid_at, known_at)) is not None
        }
        async with sessions() as session:
            actual_facts = await TemporalService(session).facts_at(
                ctx,
                entity_type="customer",
                external_id=customer,
                valid_at=valid_at,
                known_at=known_at,
            )
        actual = {f.property: f.version.id for f in actual_facts}
        if actual != expected:
            mismatches.append((valid_at, known_at, expected, actual))

    assert mismatches == []
    assert sum(len(v) for v in history.values()) >= len(properties)
