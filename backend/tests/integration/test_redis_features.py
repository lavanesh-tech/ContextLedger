"""Rate limits, idempotency keys, the retrieval cache and MCP session state, end to end.

These run against PostgreSQL with the application's store: real Redis when the
app is configured with one, the in-memory store otherwise. Redis itself is
tested in test_redis_store.py.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache.retrieval import RetrievalCache
from app.cache.store import MemoryStore
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole
from app.main import create_app
from app.mcp.state import McpSessionState
from app.mcp.tools import McpIdentity, McpRuntime, ToolHandlers
from app.providers.embeddings import DeterministicHashEmbeddingProvider
from tests.integration.factories import add_member, admin_workspace, make_user, unique
from tests.integration.support import settings_for
from tests.integration.test_mcp_tools import fact

pytestmark = pytest.mark.integration

V1 = "/api/v1"
USER = "X-ContextLedger-User-Id"
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


@pytest.fixture
async def limited_client(migrated_database: URL) -> AsyncIterator[AsyncClient]:
    settings = settings_for(migrated_database).model_copy(
        update={"rate_limit_requests_per_minute": 3, "rate_limit_token_requests_per_minute": 2}
    )
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client
    await app.state.db_engine.dispose()


async def new_user(client: AsyncClient, name: str) -> str:
    response = await client.post(
        f"{V1}/users", json={"email": f"{unique(name)}@example.com", "display_name": name}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def org_with_source(client: AsyncClient, owner: str) -> tuple[str, str]:
    org = await client.post(
        f"{V1}/organizations", headers={USER: owner}, json={"name": "Org", "slug": unique("org")}
    )
    assert org.status_code == 201, org.text
    base = f"{V1}/organizations/{org.json()['id']}"
    source = await client.post(
        f"{base}/sources",
        headers={USER: owner},
        json={"name": "billing", "source_type": "SYSTEM_OF_RECORD", "default_authority": 90},
    )
    assert source.status_code == 201, source.text
    return base, str(source.json()["id"])


def fact_body(source_id: str, value: Any, prop: str = "credit_limit", **extra: Any) -> Any:
    return {
        "entity_type": "customer",
        "external_id": "customer-991",
        "property": prop,
        "value": value,
        "source_id": source_id,
        "valid_from": T0.isoformat(),
        **extra,
    }


# --- rate limiting --------------------------------------------------------------------


async def test_each_caller_has_its_own_request_budget(limited_client: AsyncClient) -> None:
    alice = await new_user(limited_client, "alice")
    bob = await new_user(limited_client, "bob")
    me = f"{V1}/users/me"

    statuses = [(await limited_client.get(me, headers={USER: alice})).status_code for _ in range(4)]
    refused = await limited_client.get(me, headers={USER: alice})
    bob_status = (await limited_client.get(me, headers={USER: bob})).status_code

    assert statuses == [200, 200, 200, 429]
    assert refused.json()["code"] == "rate_limited"
    assert refused.headers["content-type"] == "application/problem+json"
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert refused.headers["RateLimit-Limit"] == "3"
    assert bob_status == 200


async def test_token_requests_are_limited_per_client_id(limited_client: AsyncClient) -> None:
    form = {"grant_type": "client_credentials", "client_id": "cl_unknown", "client_secret": "x"}

    statuses = [
        (await limited_client.post(f"{V1}/oauth/token", data=form)).status_code for _ in range(3)
    ]
    other = await limited_client.post(f"{V1}/oauth/token", data={**form, "client_id": "cl_other"})
    limited = await limited_client.post(f"{V1}/oauth/token", data=form)

    assert statuses == [401, 401, 429]
    assert limited.json()["error"] == "too_many_requests"
    assert int(limited.headers["Retry-After"]) >= 1
    assert other.status_code == 401


# --- idempotency ------------------------------------------------------------------------


async def test_a_retried_fact_write_is_recorded_once(db_client: AsyncClient) -> None:
    owner = await new_user(db_client, "owner")
    base, source = await org_with_source(db_client, owner)
    headers = {USER: owner, "Idempotency-Key": "fact-write-1"}

    first = await db_client.post(f"{base}/facts", headers=headers, json=fact_body(source, 5000))
    retry = await db_client.post(f"{base}/facts", headers=headers, json=fact_body(source, 5000))
    history = await db_client.get(
        f"{base}/facts/{first.json()['fact_id']}/history", headers={USER: owner}
    )
    mismatch = await db_client.post(f"{base}/facts", headers=headers, json=fact_body(source, 6000))

    assert first.status_code == retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert [v["version"] for v in history.json()] == [1]
    assert mismatch.status_code == 422


async def test_an_outsider_cannot_use_idempotency_keys_to_read_responses(
    db_client: AsyncClient,
) -> None:
    owner = await new_user(db_client, "owner")
    outsider = await new_user(db_client, "outsider")
    base, source = await org_with_source(db_client, owner)
    body = fact_body(source, 5000)

    await db_client.post(f"{base}/facts", headers={USER: owner, "Idempotency-Key": "k"}, json=body)
    stolen = await db_client.post(
        f"{base}/facts", headers={USER: outsider, "Idempotency-Key": "k"}, json=body
    )

    assert stolen.status_code == 403
    assert "Idempotent-Replayed" not in stolen.headers


# --- retrieval cache --------------------------------------------------------------------


async def test_searches_are_cached_and_writes_invalidate_them(db_client: AsyncClient) -> None:
    owner = await new_user(db_client, "owner")
    base, source = await org_with_source(db_client, owner)
    await db_client.post(f"{base}/facts", headers={USER: owner}, json=fact_body(source, 5000))
    query = {"query": "credit limit customer-991"}

    first = await db_client.post(f"{base}/search", headers={USER: owner}, json=query)
    second = await db_client.post(f"{base}/search", headers={USER: owner}, json=query)
    await db_client.post(
        f"{base}/facts",
        headers={USER: owner},
        json=fact_body(source, "Net 30", prop="payment_terms"),
    )
    after_write = await db_client.post(f"{base}/search", headers={USER: owner}, json=query)

    assert first.json()["cache"] == "miss"
    assert second.json()["cache"] == "hit"
    assert second.json()["results"] == first.json()["results"]
    assert after_write.json()["cache"] == "miss"
    assert len(after_write.json()["results"]) == 2


async def test_cached_results_never_cross_privacy_scopes(db_client: AsyncClient) -> None:
    owner = await new_user(db_client, "owner")
    viewer = await new_user(db_client, "viewer")
    base, source = await org_with_source(db_client, owner)
    add = await db_client.post(
        f"{base}/members", headers={USER: owner}, json={"user_id": viewer, "role": "VIEWER"}
    )
    assert add.status_code == 201, add.text
    await db_client.post(
        f"{base}/facts",
        headers={USER: owner},
        json=fact_body(source, "watch closely", prop="risk_notes", privacy_scope="CONFIDENTIAL"),
    )
    query = {"query": "risk notes customer-991"}

    admin_view = await db_client.post(f"{base}/search", headers={USER: owner}, json=query)
    viewer_view = await db_client.post(f"{base}/search", headers={USER: viewer}, json=query)

    assert [r["property"] for r in admin_view.json()["results"]] == ["risk_notes"]
    assert viewer_view.json()["results"] == []
    assert viewer_view.json()["cache"] == "miss"


async def test_the_cache_rechecks_membership_on_every_request(db_client: AsyncClient) -> None:
    owner = await new_user(db_client, "owner")
    member = await new_user(db_client, "member")
    base, source = await org_with_source(db_client, owner)
    await db_client.post(
        f"{base}/members", headers={USER: owner}, json={"user_id": member, "role": "ENGINEER"}
    )
    await db_client.post(f"{base}/facts", headers={USER: owner}, json=fact_body(source, 5000))
    query = {"query": "credit limit"}

    warm = await db_client.post(f"{base}/search", headers={USER: member}, json=query)
    removed = await db_client.delete(f"{base}/members/{member}", headers={USER: owner})
    after = await db_client.post(f"{base}/search", headers={USER: member}, json=query)

    assert warm.status_code == 200
    assert removed.status_code == 204
    assert after.status_code == 403


# --- MCP session state ---------------------------------------------------------------------


async def test_record_decision_defaults_to_the_sessions_last_snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    admin_ctx, source = await admin_workspace(sessions)
    await fact(sessions, admin_ctx, source, "credit_limit", 5000)
    bot = await make_user(sessions)
    await add_member(sessions, admin_ctx, bot, MembershipRole.ENGINEER)
    store = MemoryStore()

    def tools(session_id: str) -> ToolHandlers:
        return ToolHandlers(
            McpRuntime(
                sessions=sessions,
                provider=DeterministicHashEmbeddingProvider(),
                identity=McpIdentity(
                    admin_ctx.organization_id, bot.id, "agent", PrivacyScope.INTERNAL
                ),
                cache=RetrievalCache(store),
                state=McpSessionState(
                    store,
                    organization_id=admin_ctx.organization_id,
                    user_id=bot.id,
                    agent_name="agent",
                    session_id=session_id,
                ),
            )
        )

    session = tools("s1")
    first_search = await session.search_facts("credit limit")
    second_search = await session.search_facts("credit limit")
    context = await session.capture_decision_context("credit limit")
    remembered = await session.get_session_context()
    decision = await session.record_decision(
        action="credit.approve_increase",
        outcome={"approved": True},
        relied_on=[UUID(context["facts"][0]["fact_version_id"])],
    )
    receipt = await session.get_decision_receipt(UUID(decision["decision_id"]))

    assert (first_search["cache"], second_search["cache"]) == ("miss", "hit")
    assert remembered["last_snapshot_id"] == context["snapshot_id"]
    assert remembered["recent_queries"] == ["credit limit"]
    assert receipt["context"]["snapshot_id"] == context["snapshot_id"]
    with pytest.raises(ToolError, match="capture_decision_context first"):
        await tools("s2").record_decision(action="a.b", outcome={}, relied_on=[])
