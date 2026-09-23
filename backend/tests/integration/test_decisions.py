"""Decision receipts against real PostgreSQL."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.models.source import FactSource
from app.providers.embeddings import DeterministicHashEmbeddingProvider
from app.services.decisions import (
    CapturedContext,
    DecisionReceipt,
    DecisionService,
    RecordDecision,
)
from app.services.evidence import EvidenceService
from app.services.facts import RecordFactVersion
from app.services.retrieval import RetrievalQuery
from tests.integration.factories import (
    add_member,
    admin_workspace,
    capture,
    context,
    make_user,
    record,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
PROVIDER = DeterministicHashEmbeddingProvider()


async def fact(
    sessions: Sessions,
    ctx: TenantContext,
    source: FactSource,
    prop: str,
    value: object,
    *,
    hours: int = 0,
    **extra: Any,
) -> Any:
    return await record(
        sessions,
        ctx,
        RecordFactVersion(
            entity_type="customer",
            external_id="customer-991",
            property=prop,
            value=value,
            source_id=source.id,
            valid_from=T0 + timedelta(hours=hours),
            **extra,
        ),
    )


async def capture_context(
    sessions: Sessions, ctx: TenantContext, query: str = "credit limit customer-991", **kw: Any
) -> CapturedContext:
    async with sessions() as session:
        return await DecisionService(session, PROVIDER).capture_context(
            ctx, RetrievalQuery(query=query, **kw)
        )


async def decide(
    sessions: Sessions, ctx: TenantContext, captured: CapturedContext, **kw: Any
) -> DecisionReceipt:
    relied_on = kw.pop("relied_on", [r.version.id for r in captured.retrieval.results[:1]])
    async with sessions() as session:
        return await DecisionService(session, PROVIDER).record_decision(
            ctx,
            RecordDecision(
                snapshot_id=captured.snapshot_id,
                action=kw.pop("action", "credit.approve_increase"),
                outcome=kw.pop("outcome", {"approved": True, "new_limit": 7500}),
                relied_on=relied_on,
                rationale=kw.pop("rationale", "Payment history supports the increase."),
                agent=kw.pop("agent", "credit-review-agent v3"),
            ),
        )


async def read_receipt(sessions: Sessions, ctx: TenantContext, decision_id: Any) -> DecisionReceipt:
    async with sessions() as session:
        return await DecisionService(session, PROVIDER).receipt(ctx, decision_id)


async def workspace(sessions: Sessions) -> tuple[TenantContext, FactSource, Any]:
    ctx, source = await admin_workspace(sessions)
    limit = await fact(sessions, ctx, source, "credit_limit", 2000)
    await fact(sessions, ctx, source, "shipping_city", "Berlin")
    return ctx, source, limit


# --- the happy path ------------------------------------------------------------------


async def test_receipt_records_decision_context_and_reliance(sessions: Sessions) -> None:
    ctx, _, limit = await workspace(sessions)

    captured = await capture_context(sessions, ctx)
    receipt = await decide(sessions, ctx, captured, relied_on=[limit.id])

    assert receipt.integrity_verified
    assert receipt.action == "credit.approve_increase"
    assert receipt.outcome == {"approved": True, "new_limit": 7500}
    assert receipt.agent == "credit-review-agent v3"
    assert receipt.decided_by_user_id == ctx.user_id
    assert receipt.context.snapshot_id == captured.snapshot_id
    assert receipt.context.known_at == captured.known_at
    assert receipt.context.parameters["limit"] == 10
    relied = [f for f in receipt.facts if f.relied_on]
    assert [(f.property, f.version.value) for f in relied if f.version] == [("credit_limit", 2000)]
    assert [f.position for f in receipt.facts] == list(range(1, len(receipt.facts) + 1))
    assert all(f.ranking["score"] > 0 for f in receipt.facts)


async def test_receipt_keeps_the_facts_as_they_were_known(sessions: Sessions) -> None:
    ctx, source, limit = await workspace(sessions)
    captured = await capture_context(sessions, ctx)
    receipt = await decide(sessions, ctx, captured, relied_on=[limit.id])

    # Later the limit changes; the receipt must not change with it.
    await fact(sessions, ctx, source, "credit_limit", 9000, hours=5)
    later = await read_receipt(sessions, ctx, receipt.decision_id)

    relied = next(f for f in later.facts if f.relied_on)
    assert relied.version is not None
    assert relied.version.value == 2000
    assert relied.version.valid_until is None  # its end was not known when deciding
    assert later.integrity_verified
    assert later.receipt_sha256 == receipt.receipt_sha256


async def test_evidence_in_the_receipt_is_what_existed_at_decision_time(
    sessions: Sessions,
) -> None:
    ctx, source = await admin_workspace(sessions)
    before = await capture(sessions, ctx, source, "Contract: credit limit 2000 USD.")
    limit = await fact(
        sessions, ctx, source, "credit_limit", 2000, evidence_ids=(before.evidence.id,)
    )
    captured = await capture_context(sessions, ctx)
    receipt = await decide(sessions, ctx, captured, relied_on=[limit.id])

    after = await capture(sessions, ctx, source, "Later memo: limit confirmed.")
    async with sessions() as session:
        await EvidenceService(session).attach(
            ctx, fact_version_id=limit.id, evidence_id=after.evidence.id
        )
    reread = await read_receipt(sessions, ctx, receipt.decision_id)

    relied = next(f for f in reread.facts if f.relied_on)
    assert [e.evidence_id for e in relied.evidence] == [before.evidence.id]
    assert relied.evidence[0].content_sha256 == before.evidence.content_sha256


async def test_decisions_relying_on_a_fact(sessions: Sessions) -> None:
    ctx, _, limit = await workspace(sessions)
    captured = await capture_context(sessions, ctx)
    first = await decide(sessions, ctx, captured, relied_on=[limit.id])
    second = await decide(sessions, ctx, captured, relied_on=[], action="credit.defer_review")

    async with sessions() as session:
        ids = await DecisionService(session, PROVIDER).decisions_relying_on(ctx, limit.id)

    assert ids == [first.decision_id]
    assert second.decision_id not in ids


# --- integrity ------------------------------------------------------------------------


async def test_tampering_is_detected(sessions: Sessions, engine: AsyncEngine) -> None:
    ctx, _, limit = await workspace(sessions)
    receipt = await decide(
        sessions, ctx, await capture_context(sessions, ctx), relied_on=[limit.id]
    )

    # Someone with table-owner rights bypasses the append-only trigger.
    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE decisions DISABLE TRIGGER trg_decisions_immutable")
        )
        await connection.execute(
            text("UPDATE decisions SET outcome = '{\"approved\": false}' WHERE id = :id"),
            {"id": receipt.decision_id},
        )
        await connection.execute(
            text("ALTER TABLE decisions ENABLE TRIGGER trg_decisions_immutable")
        )

    tampered = await read_receipt(sessions, ctx, receipt.decision_id)

    assert tampered.outcome == {"approved": False}
    assert not tampered.integrity_verified


@pytest.mark.parametrize(
    "table", ["context_snapshots", "context_snapshot_facts", "decisions", "decision_facts"]
)
async def test_receipt_tables_are_append_only(
    sessions: Sessions, engine: AsyncEngine, table: str
) -> None:
    ctx, _, limit = await workspace(sessions)
    await decide(sessions, ctx, await capture_context(sessions, ctx), relied_on=[limit.id])

    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as connection:
            await connection.execute(
                text(f"DELETE FROM {table} WHERE organization_id = :o"),
                {"o": ctx.organization_id},
            )


async def test_database_rejects_citing_a_fact_outside_the_snapshot(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    ctx, _, limit = await workspace(sessions)
    captured = await capture_context(sessions, ctx, query="shipping city", limit=1)
    receipt = await decide(sessions, ctx, captured, relied_on=[])
    assert limit.id not in {r.version.id for r in captured.retrieval.results}

    with pytest.raises(
        IntegrityError, match="fk_decision_facts_snapshot_id_context_snapshot_facts"
    ):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO decision_facts (organization_id, decision_id, snapshot_id, "
                    "fact_version_id) VALUES (:o, :d, :s, :v)"
                ),
                {
                    "o": ctx.organization_id,
                    "d": receipt.decision_id,
                    "s": captured.snapshot_id,
                    "v": limit.id,
                },
            )


# --- validation, permissions, tenants -------------------------------------------------


async def test_relied_on_must_be_in_the_snapshot(sessions: Sessions) -> None:
    ctx, _, limit = await workspace(sessions)
    captured = await capture_context(sessions, ctx, query="shipping city", limit=1)

    with pytest.raises(ValidationFailedError, match="context snapshot"):
        await decide(sessions, ctx, captured, relied_on=[limit.id])


@pytest.mark.parametrize(
    "kwargs", [{"action": "Approve it!"}, {"outcome": None}, {"rationale": "x" * 4001}]
)
async def test_invalid_decisions_are_rejected(sessions: Sessions, kwargs: dict[str, Any]) -> None:
    ctx, _, _ = await workspace(sessions)
    captured = await capture_context(sessions, ctx)

    with pytest.raises(ValidationFailedError):
        await decide(sessions, ctx, captured, **kwargs)


async def test_viewers_can_read_but_not_record(sessions: Sessions) -> None:
    ctx, source, limit = await workspace(sessions)
    secret = await fact(
        sessions,
        ctx,
        source,
        "credit_limit_override",
        50_000,
        privacy_scope=PrivacyScope.RESTRICTED,
    )
    captured = await capture_context(sessions, ctx, query="credit limit override customer-991")
    receipt = await decide(sessions, ctx, captured, relied_on=[limit.id, secret.id])
    viewer = await make_user(sessions)
    await add_member(sessions, ctx, viewer, MembershipRole.VIEWER)
    viewer_ctx = await context(sessions, ctx.organization_id, viewer.id)

    with pytest.raises(PermissionDeniedError):
        await capture_context(sessions, viewer_ctx)
    seen = await read_receipt(sessions, viewer_ctx, receipt.decision_id)

    hidden = next(f for f in seen.facts if f.fact_version_id == secret.id)
    assert hidden.redacted
    assert hidden.relied_on
    assert hidden.version is None
    assert hidden.property is None
    assert seen.integrity_verified  # redaction does not change what was sealed
    visible = next(f for f in seen.facts if f.fact_version_id == limit.id)
    assert not visible.redacted


async def test_other_tenants_cannot_see_or_use_receipts(sessions: Sessions) -> None:
    ctx, _, limit = await workspace(sessions)
    captured = await capture_context(sessions, ctx)
    receipt = await decide(sessions, ctx, captured, relied_on=[limit.id])
    other_ctx, _ = await admin_workspace(sessions)

    with pytest.raises(NotFoundError):
        await read_receipt(sessions, other_ctx, receipt.decision_id)
    with pytest.raises(NotFoundError):
        await decide(sessions, other_ctx, captured, relied_on=[])


async def test_unknown_ids(sessions: Sessions) -> None:
    ctx, _, _ = await workspace(sessions)

    with pytest.raises(NotFoundError):
        await read_receipt(sessions, ctx, uuid4())
    fake = CapturedContext(
        snapshot_id=uuid4(),
        valid_at=T0,
        known_at=T0,
        retrieval=(await capture_context(sessions, ctx)).retrieval,
    )
    with pytest.raises(NotFoundError):
        await decide(sessions, ctx, fake, relied_on=[])


async def test_explicit_known_at_is_respected(sessions: Sessions, engine: AsyncEngine) -> None:
    ctx, source, _ = await workspace(sessions)
    async with engine.connect() as connection:
        before_change = await connection.scalar(text("SELECT clock_timestamp()"))
    await fact(sessions, ctx, source, "credit_limit", 9000, hours=5)

    captured = await capture_context(
        sessions, ctx, valid_at=T0 + timedelta(hours=6), known_at=before_change
    )

    values = {(r.property, r.version.value) for r in captured.retrieval.results}
    assert ("credit_limit", 2000) in values
    assert ("credit_limit", 9000) not in values
    assert captured.known_at == before_change
