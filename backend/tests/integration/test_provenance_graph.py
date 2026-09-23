"""Outbox → Neo4j projection and provenance traversals (PostgreSQL + Neo4j)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import NotFoundError, PermissionDeniedError
from app.domain.evidence import EvidenceRelation
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.models.outbox import GraphOutboxEvent
from app.models.source import FactSource
from app.providers.embeddings import DeterministicHashEmbeddingProvider
from app.repositories.graph_outbox import PROJECTED
from app.services.decisions import DecisionService, RecordDecision
from app.services.evidence import EvidenceService
from app.services.facts import RecordFactVersion
from app.services.provenance import ProvenanceService
from app.services.retrieval import RetrievalQuery
from app.workers.graph import GraphProjector
from tests.integration.conftest import GraphHarness
from tests.integration.factories import (
    add_member,
    admin_workspace,
    capture,
    context,
    make_source,
    make_user,
    record,
)

pytestmark = [pytest.mark.integration, pytest.mark.graph]

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
PROVIDER = DeterministicHashEmbeddingProvider()


async def version(
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


async def decide(
    sessions: Sessions, ctx: TenantContext, query: str, relied_on: Sequence[Any]
) -> Any:
    async with sessions() as session:
        captured = await DecisionService(session, PROVIDER).capture_context(
            ctx, RetrievalQuery(query=query)
        )
    async with sessions() as session:
        return await DecisionService(session, PROVIDER).record_decision(
            ctx,
            RecordDecision(
                snapshot_id=captured.snapshot_id,
                action="credit.approve_increase",
                outcome={"approved": True},
                relied_on=[v.id for v in relied_on],
            ),
        )


def provenance(sessions: Sessions, graph: GraphHarness) -> Any:
    class Scoped:
        async def __aenter__(self) -> ProvenanceService:
            self._session = sessions()
            session = await self._session.__aenter__()
            return ProvenanceService(session, graph.reader)

        async def __aexit__(self, *exc: object) -> None:
            await self._session.__aexit__(*exc)

    return Scoped()


async def story(sessions: Sessions, graph: GraphHarness) -> Mapping[str, Any]:
    """billing asserts a limit (2000 → 5000), a contract backs 2000, a decision relies on it."""
    ctx, billing = await admin_workspace(sessions)
    contract = await capture(sessions, ctx, billing, "Contract: credit limit 2000 USD.")
    v1 = await version(
        sessions, ctx, billing, "credit_limit", 2000, evidence_ids=(contract.evidence.id,)
    )
    city = await version(sessions, ctx, billing, "shipping_city", "Berlin")
    decision = await decide(sessions, ctx, "credit limit customer-991", [v1])
    v2 = await version(sessions, ctx, billing, "credit_limit", 5000, hours=5)
    projector = graph.projector(sessions, ctx.organization_id)
    await projector.drain()
    return {
        "ctx": ctx,
        "billing": billing,
        "contract": contract.evidence,
        "v1": v1,
        "v2": v2,
        "city": city,
        "decision": decision,
        "projector": projector,
    }


# --- outbox ---------------------------------------------------------------------------


async def test_every_write_lands_in_the_outbox(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await version(sessions, ctx, source, "credit_limit", 2000)

    async with sessions() as session:
        tables = set(
            (
                await session.scalars(
                    select(GraphOutboxEvent.table_name).where(
                        GraphOutboxEvent.organization_id == ctx.organization_id
                    )
                )
            ).all()
        )

    assert {"fact_sources", "entities", "facts", "fact_versions"} <= tables


async def test_a_failed_graph_write_leaves_the_events_queued(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await version(sessions, ctx, source, "credit_limit", 2000)

    class BrokenWriter:
        async def apply(self, rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]]) -> int:
            raise ConnectionError("neo4j unavailable")

    before = await pending(sessions, ctx)
    with pytest.raises(ConnectionError):
        await GraphProjector(
            sessions, BrokenWriter(), organization_id=ctx.organization_id
        ).run_once()

    assert await pending(sessions, ctx) == before > 0


async def pending(sessions: Sessions, ctx: TenantContext) -> int:
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(GraphOutboxEvent)
            .where(GraphOutboxEvent.organization_id == ctx.organization_id)
        )
    return int(count or 0)


# --- projection -----------------------------------------------------------------------


async def test_projection_builds_the_provenance_graph(
    sessions: Sessions, graph: GraphHarness
) -> None:
    s = await story(sessions, graph)
    org = str(s["ctx"].organization_id)

    [row] = await graph.query(
        """
        MATCH (d:Decision {id: $d, org: $org})-[:RELIED_ON]->(v:FactVersion)
              -[:VERSION_OF]->(:Fact {property: 'credit_limit'})-[:OF_ENTITY]->(e:Entity)
        MATCH (v)-[:ASSERTED_BY]->(s:Source)
        MATCH (x:Evidence)-[l:EVIDENCE_FOR]->(v)
        MATCH (d)-[:BASED_ON]->(:Snapshot)-[i:INCLUDED]->(v)
        MATCH (later:FactVersion)-[:SUPERSEDES]->(v)
        RETURN v.id AS v, e.external_id AS entity, s.id AS source, x.id AS evidence,
               l.relation AS relation, i.position AS position, later.id AS later,
               v.valid_until IS NOT NULL AS closed
        """,
        d=str(s["decision"].decision_id),
        org=org,
    )

    assert row["v"] == str(s["v1"].id)
    assert row["entity"] == "customer-991"
    assert row["source"] == str(s["billing"].id)
    assert row["evidence"] == str(s["contract"].id)
    assert row["relation"] == "SUPPORTS"
    assert row["position"] >= 1
    assert row["later"] == str(s["v2"].id)
    assert row["closed"]  # the UPDATE that closed v1 was projected too
    assert await pending(sessions, s["ctx"]) == 0


async def requeue_everything(sessions: Sessions, ctx: TenantContext) -> None:
    """Queue every row of the organization again, exactly like the migration backfill."""
    async with sessions() as session, session.begin():
        for table, (_, columns) in PROJECTED.items():
            key = ", ".join(f"'{c}', {c}" for c in columns)
            await session.execute(
                text(
                    "INSERT INTO graph_outbox (organization_id, table_name, row_key) "
                    f"SELECT organization_id, '{table}', jsonb_build_object({key}) "
                    f"FROM {table} WHERE organization_id = :o"
                ),
                {"o": ctx.organization_id},
            )


async def test_projection_is_idempotent(sessions: Sessions, graph: GraphHarness) -> None:
    s = await story(sessions, graph)
    org = str(s["ctx"].organization_id)
    count = (
        "MATCH (n {org: $org}) OPTIONAL MATCH (n)-[r]->() "
        "RETURN count(DISTINCT n) AS nodes, count(r) AS relationships"
    )
    before = await graph.query(count, org=org)

    await requeue_everything(sessions, s["ctx"])
    replayed = await s["projector"].drain()

    assert replayed.events > 0
    assert await graph.query(count, org=org) == before


# --- impact ---------------------------------------------------------------------------


async def test_impact_of_a_fact_version(sessions: Sessions, graph: GraphHarness) -> None:
    s = await story(sessions, graph)

    async with provenance(sessions, graph) as service:
        report = await service.impact_of_fact_version(s["ctx"], s["v1"].id)
        city = await service.impact_of_fact_version(s["ctx"], s["city"].id)

    assert [d.decision_id for d in report.decisions] == [s["decision"].decision_id]
    assert report.decisions[0].because_of == ((s["v1"].id, "relied_on"),)
    assert report.superseded_by == (s["v2"].id,)
    assert report.pending_events == 0
    # The city was in the decision's context but not relied on.
    assert [d.because_of for d in city.decisions] == [((s["city"].id, "in_context"),)]


async def test_impact_of_a_source_follows_assertions_and_evidence(
    sessions: Sessions, graph: GraphHarness
) -> None:
    s = await story(sessions, graph)
    ctx = s["ctx"]
    # A second source whose document is the only support for the relied-on version.
    auditor = await make_source(sessions, ctx, default_authority=70)
    memo = await capture(sessions, ctx, auditor, "Audit memo: limit 2000 confirmed.")
    async with sessions() as session:
        await EvidenceService(session).attach(
            ctx, fact_version_id=s["v1"].id, evidence_id=memo.evidence.id
        )
    await s["projector"].drain()

    async with provenance(sessions, graph) as service:
        billing = await service.impact_of_source(ctx, s["billing"].id)
        audit = await service.impact_of_source(ctx, auditor.id)

    assert set(billing.affected_versions) >= {s["v1"].id, s["v2"].id, s["city"].id}
    assert [d.decision_id for d in billing.decisions] == [s["decision"].decision_id]
    assert audit.affected_versions == (s["v1"].id,)
    assert audit.decisions[0].because_of == ((s["v1"].id, "supported_by_source_evidence"),)


async def test_impact_of_evidence(sessions: Sessions, graph: GraphHarness) -> None:
    s = await story(sessions, graph)
    ctx = s["ctx"]
    rebuttal = await capture(sessions, ctx, s["billing"], "Email: the 2000 limit was a typo.")
    async with sessions() as session:
        await EvidenceService(session).attach(
            ctx,
            fact_version_id=s["v1"].id,
            evidence_id=rebuttal.evidence.id,
            relation=EvidenceRelation.CONTRADICTS,
        )
    await s["projector"].drain()

    async with provenance(sessions, graph) as service:
        contract = await service.impact_of_evidence(ctx, s["contract"].id)
        contradiction = await service.impact_of_evidence(ctx, rebuttal.evidence.id)

    assert contract.decisions[0].because_of == ((s["v1"].id, "supports"),)
    assert contradiction.decisions[0].because_of == ((s["v1"].id, "contradicts"),)


async def test_decision_lineage(sessions: Sessions, graph: GraphHarness) -> None:
    s = await story(sessions, graph)

    async with provenance(sessions, graph) as service:
        lineage = await service.decision_lineage(s["ctx"], s["decision"].decision_id)

    [step] = lineage.relied_on
    assert step.fact_version_id == s["v1"].id
    assert (step.entity_type, step.external_id, step.property) == (
        "customer",
        "customer-991",
        "credit_limit",
    )
    assert step.source_id == s["billing"].id
    assert [e.evidence_id for e in step.evidence] == [s["contract"].id]
    assert not step.redacted


# --- tenants and permissions ------------------------------------------------------------


async def test_other_tenants_see_nothing(sessions: Sessions, graph: GraphHarness) -> None:
    s = await story(sessions, graph)
    other, _ = await admin_workspace(sessions)

    async with provenance(sessions, graph) as service:
        with pytest.raises(NotFoundError):
            await service.impact_of_fact_version(other, s["v1"].id)
        with pytest.raises(NotFoundError):
            await service.decision_lineage(other, s["decision"].decision_id)


async def test_non_members_are_rejected(sessions: Sessions, graph: GraphHarness) -> None:
    s = await story(sessions, graph)
    outsider = await make_user(sessions)
    stranger = TenantContext(s["ctx"].organization_id, outsider.id, MembershipRole.ADMIN)

    async with provenance(sessions, graph) as service:
        with pytest.raises(PermissionDeniedError):
            await service.impact_of_source(stranger, s["billing"].id)


async def test_lineage_redacts_facts_above_the_readers_scope(
    sessions: Sessions, graph: GraphHarness
) -> None:
    ctx, source = await admin_workspace(sessions)
    secret = await version(
        sessions, ctx, source, "credit_limit", 2000, privacy_scope=PrivacyScope.RESTRICTED
    )
    decision = await decide(sessions, ctx, "credit limit customer-991", [secret])
    await graph.projector(sessions, ctx.organization_id).drain()
    viewer = await make_user(sessions)
    await add_member(sessions, ctx, viewer, MembershipRole.VIEWER)
    viewer_ctx = await context(sessions, ctx.organization_id, viewer.id)

    async with provenance(sessions, graph) as service:
        lineage = await service.decision_lineage(viewer_ctx, decision.decision_id)

    [step] = lineage.relied_on
    assert step.redacted
    assert step.property is None
    assert step.source_id is None


async def test_pending_events_are_reported(sessions: Sessions, graph: GraphHarness) -> None:
    s = await story(sessions, graph)
    await version(sessions, s["ctx"], s["billing"], "risk_rating", "low")  # not projected yet

    async with provenance(sessions, graph) as service:
        report = await service.impact_of_source(s["ctx"], s["billing"].id)

    assert report.pending_events > 0
