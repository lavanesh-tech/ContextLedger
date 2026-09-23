"""Agents, OAuth2 client credentials, scopes, privacy ceilings and revocation (REST)."""

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import URL

from app.main import create_app
from tests.integration.factories import unique
from tests.integration.support import settings_for

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
V1 = "/api/v1"
HEADER = "X-ContextLedger-User-Id"


async def call(
    client: AsyncClient,
    method: str,
    path: str,
    *,
    expect: int,
    user: str | None = None,
    token: str | None = None,
    **kwargs: Any,
) -> Any:
    headers = dict(kwargs.pop("headers", {}))
    if user is not None:
        headers[HEADER] = user
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.request(method, V1 + path, headers=headers, **kwargs)
    assert response.status_code == expect, response.text
    return response.json() if response.content else None


async def setup(client: AsyncClient) -> Mapping[str, Any]:
    """Alice (ADMIN) with an organization, a source and one INTERNAL and one PUBLIC fact."""
    alice = (
        await call(
            client,
            "POST",
            "/users",
            json={"email": f"{unique('alice')}@example.com", "display_name": "Alice"},
            expect=201,
        )
    )["id"]
    org = await call(
        client,
        "POST",
        "/organizations",
        user=alice,
        json={"name": "Acme", "slug": unique("acme")},
        expect=201,
    )
    base = f"/organizations/{org['id']}"
    source = await call(
        client,
        "POST",
        f"{base}/sources",
        user=alice,
        json={"name": "billing-db", "source_type": "SYSTEM_OF_RECORD"},
        expect=201,
    )
    for prop, value, scope in (
        ("credit_limit", 5000, "INTERNAL"),
        ("company_name", "Acme Ltd", "PUBLIC"),
    ):
        await call(
            client,
            "POST",
            f"{base}/facts",
            user=alice,
            json={
                "entity_type": "customer",
                "external_id": "customer-991",
                "property": prop,
                "value": value,
                "source_id": source["id"],
                "valid_from": T0.isoformat(),
                "privacy_scope": scope,
            },
            expect=201,
        )
    return {"alice": alice, "org": org["id"], "base": base}


async def agent(
    client: AsyncClient, s: Mapping[str, Any], *, scope: str | None = None, **spec: Any
) -> tuple[dict[str, Any], str]:
    body = {"name": unique("agent"), "role": "ENGINEER", "scopes": ["facts:read"], **spec}
    created = await call(
        client, "POST", f"{s['base']}/agent-clients", user=s["alice"], json=body, expect=201
    )
    form = {
        "grant_type": "client_credentials",
        "client_id": created["client_id"],
        "client_secret": created["client_secret"],
    }
    if scope is not None:
        form["scope"] = scope
    token = await call(client, "POST", "/oauth/token", data=form, expect=200)
    assert token["token_type"] == "Bearer"
    return created, token["access_token"]


# --- client credentials ---------------------------------------------------------------------


async def test_agents_authenticate_with_client_credentials(db_client: AsyncClient) -> None:
    s = await setup(db_client)
    created, token = await agent(db_client, s)

    facts = await call(
        db_client,
        "GET",
        f"{s['base']}/entities/customer/customer-991/facts",
        token=token,
        expect=200,
    )
    listed = await call(db_client, "GET", f"{s['base']}/agent-clients", user=s["alice"], expect=200)

    assert {f["property"] for f in facts["facts"]} == {"credit_limit", "company_name"}
    assert "client_secret" not in listed[0]
    assert listed[0]["client_id"] == created["client_id"]


@pytest.mark.parametrize(
    ("form", "status", "error"),
    [
        ({"client_secret": "cs_wrong"}, 401, "invalid_client"),
        ({"client_id": "cl_unknown"}, 401, "invalid_client"),
        ({"grant_type": "password"}, 400, "unsupported_grant_type"),
        ({"scope": "facts:write"}, 400, "invalid_scope"),
        ({"scope": "not-a-scope"}, 400, "invalid_scope"),
    ],
)
async def test_token_endpoint_errors_follow_rfc_6749(
    db_client: AsyncClient, form: dict[str, str], status: int, error: str
) -> None:
    s = await setup(db_client)
    created = await call(
        db_client,
        "POST",
        f"{s['base']}/agent-clients",
        user=s["alice"],
        json={"name": unique("agent"), "scopes": ["facts:read"]},
        expect=201,
    )
    data = {
        "grant_type": "client_credentials",
        "client_id": created["client_id"],
        "client_secret": created["client_secret"],
        **form,
    }

    response = await db_client.post(f"{V1}/oauth/token", data=data)

    assert response.status_code == status
    assert response.json()["error"] == error
    assert response.headers["Cache-Control"] == "no-store"


# --- what an agent may do ---------------------------------------------------------------------


async def test_scopes_narrow_the_role(db_client: AsyncClient) -> None:
    s = await setup(db_client)
    _, reader = await agent(
        db_client, s, scopes=["facts:read", "decisions:record"], scope="facts:read"
    )

    denied = await call(
        db_client,
        "POST",
        f"{s['base']}/context-snapshots",
        token=reader,
        json={"query": "credit"},
        expect=403,
    )

    assert "token scope" in denied["detail"]


async def test_roles_bound_what_an_agent_can_be_granted(db_client: AsyncClient) -> None:
    s = await setup(db_client)

    too_much = await call(
        db_client,
        "POST",
        f"{s['base']}/agent-clients",
        user=s["alice"],
        json={"name": unique("agent"), "role": "VIEWER", "scopes": ["decisions:record"]},
        expect=422,
    )
    admin = await call(
        db_client,
        "POST",
        f"{s['base']}/agent-clients",
        user=s["alice"],
        json={"name": unique("agent"), "role": "ADMIN", "scopes": ["facts:read"]},
        expect=422,
    )

    assert "cannot grant" in too_much["detail"]
    assert "never ADMIN" in admin["detail"]


async def test_the_agent_privacy_ceiling_applies_to_every_read(db_client: AsyncClient) -> None:
    s = await setup(db_client)
    _, token = await agent(db_client, s, privacy_ceiling="PUBLIC")

    facts = await call(
        db_client,
        "GET",
        f"{s['base']}/entities/customer/customer-991/facts",
        token=token,
        expect=200,
    )
    found = await call(
        db_client,
        "POST",
        f"{s['base']}/search",
        token=token,
        json={"query": "credit limit company"},
        expect=200,
    )

    assert [f["property"] for f in facts["facts"]] == ["company_name"]
    assert facts["withheld_by_privacy_scope"] == 1
    assert {r["property"] for r in found["results"]} == {"company_name"}
    assert found["privacy_scopes"] == ["PUBLIC"]


async def test_agent_tokens_are_bound_to_their_organization(db_client: AsyncClient) -> None:
    a = await setup(db_client)
    b = await setup(db_client)
    _, token = await agent(db_client, a)

    denied = await call(db_client, "GET", f"{b['base']}/sources", token=token, expect=403)
    no_orgs = await call(
        db_client,
        "POST",
        "/organizations",
        token=token,
        json={"name": "X", "slug": unique("x")},
        expect=403,
    )

    assert denied["code"] == "permission_denied"
    assert no_orgs["code"] == "permission_denied"


async def test_revocation_takes_effect_before_the_token_expires(db_client: AsyncClient) -> None:
    s = await setup(db_client)
    created, token = await agent(db_client, s)
    path = f"{s['base']}/sources"
    await call(db_client, "GET", path, token=token, expect=200)

    revoked = await call(
        db_client,
        "DELETE",
        f"{s['base']}/agent-clients/{created['id']}",
        user=s["alice"],
        expect=200,
    )

    assert revoked["revoked_at"] is not None
    await call(db_client, "GET", path, token=token, expect=403)
    retry = await db_client.post(
        f"{V1}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": created["client_id"],
            "client_secret": created["client_secret"],
        },
    )
    assert retry.status_code == 401


async def test_an_agent_promoted_to_admin_is_refused(db_client: AsyncClient) -> None:
    s = await setup(db_client)
    created, token = await agent(db_client, s)
    await call(
        db_client,
        "PATCH",
        f"{s['base']}/members/{created['service_user_id']}",
        user=s["alice"],
        json={"role": "ADMIN"},
        expect=200,
    )

    denied = await call(db_client, "GET", f"{s['base']}/sources", token=token, expect=403)

    assert "ADMIN" in denied["detail"]


async def test_only_admins_manage_agents(db_client: AsyncClient) -> None:
    s = await setup(db_client)
    bob = (
        await call(
            db_client,
            "POST",
            "/users",
            json={"email": f"{unique('bob')}@example.com", "display_name": "Bob"},
            expect=201,
        )
    )["id"]
    await call(
        db_client,
        "POST",
        f"{s['base']}/members",
        user=s["alice"],
        json={"user_id": bob, "role": "ENGINEER"},
        expect=201,
    )

    await call(
        db_client,
        "POST",
        f"{s['base']}/agent-clients",
        user=bob,
        json={"name": unique("agent"), "scopes": ["facts:read"]},
        expect=403,
    )


# --- users with JWTs --------------------------------------------------------------------------


@pytest.fixture
async def jwt_client(migrated_database: URL) -> AsyncIterator[AsyncClient]:
    settings = settings_for(migrated_database).model_copy(update={"auth_mode": "jwt"})
    app: FastAPI = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await app.state.db_engine.dispose()


async def test_jwt_mode_requires_bearer_tokens(jwt_client: AsyncClient) -> None:
    user = (
        await call(
            jwt_client,
            "POST",
            "/users",
            json={"email": f"{unique('u')}@example.com", "display_name": "U"},
            expect=201,
        )
    )["id"]

    await call(jwt_client, "GET", "/users/me", user=user, expect=401)  # headers ignored
    token = (await call(jwt_client, "POST", "/auth/dev-token", json={"user_id": user}, expect=200))[
        "access_token"
    ]
    me = await call(jwt_client, "GET", "/users/me", token=token, expect=200)
    bad = await call(jwt_client, "GET", "/users/me", token=token + "x", expect=401)
    unknown = await call(
        jwt_client, "POST", "/auth/dev-token", json={"user_id": str(uuid4())}, expect=404
    )

    assert me["id"] == user
    assert bad["code"] == "authentication_required"
    assert unknown["code"] == "not_found"
