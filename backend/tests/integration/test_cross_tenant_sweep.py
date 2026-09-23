"""Every tenant-scoped route refuses callers from another organization.

The routes are discovered from the application, not listed by hand, so an
endpoint added later is covered automatically. Each route is called with a
plausible request by: a member of another organization, an agent token of
another organization, and a registered user with no memberships at all.
Anything other than 403 fails the test.
"""

import re
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.integration.factories import unique

pytestmark = pytest.mark.integration

V1 = "/api/v1"
HEADER = "X-ContextLedger-User-Id"
PLACEHOLDER = re.compile(r"\{([^}]+)\}")
PATH_VALUES = {"entity_type": "customer", "external_id": "customer-991"}


def tenant_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) under an organization, from the app's own OpenAPI document."""
    return sorted(
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "{organization_id}" in path
        for method in operations
    )


async def user(client: AsyncClient, name: str) -> str:
    response = await client.post(
        f"{V1}/users", json={"email": f"{unique(name)}@example.com", "display_name": name}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def organization(client: AsyncClient, owner: str) -> str:
    response = await client.post(
        f"{V1}/organizations",
        headers={HEADER: owner},
        json={"name": "Org", "slug": unique("org")},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def agent_token(client: AsyncClient, owner: str, org: str) -> str:
    created = await client.post(
        f"{V1}/organizations/{org}/agent-clients",
        headers={HEADER: owner},
        json={
            "name": unique("agent"),
            "role": "ENGINEER",
            "scopes": ["facts:read", "facts:write", "decisions:read", "decisions:record"],
            "privacy_ceiling": "CONFIDENTIAL",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    token = await client.post(
        f"{V1}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": body["client_id"],
            "client_secret": body["client_secret"],
        },
    )
    return str(token.json()["access_token"])


async def test_every_tenant_route_rejects_other_tenants(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    victim_owner = await user(db_client, "victim")
    victim_org = await organization(db_client, victim_owner)
    attacker = await user(db_client, "attacker")
    attacker_org = await organization(db_client, attacker)
    loner = await user(db_client, "loner")
    callers: dict[str, dict[str, str]] = {
        "member of another organization": {HEADER: attacker},
        "agent of another organization": {
            "Authorization": f"Bearer {await agent_token(db_client, attacker, attacker_org)}"
        },
        "user without memberships": {HEADER: loner},
    }
    routes = tenant_routes(db_app)
    assert len(routes) >= 25, "the sweep should cover the whole tenant API"

    failures: list[str] = []
    for method, template in routes:
        path = PLACEHOLDER.sub(
            lambda m: (
                victim_org
                if m.group(1) == "organization_id"
                else PATH_VALUES.get(m.group(1), str(uuid4()))
            ),
            template,
        )
        kwargs: dict[str, Any] = {"json": {}} if method in {"POST", "PATCH", "PUT"} else {}
        for caller, headers in callers.items():
            response = await db_client.request(method, path, headers=headers, **kwargs)
            if response.status_code != 403:
                failures.append(f"{method} {template} as {caller}: {response.status_code}")

    assert not failures, "\n".join(failures)
