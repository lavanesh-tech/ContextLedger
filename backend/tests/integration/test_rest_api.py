"""The v1 REST API end to end against PostgreSQL (and Neo4j for provenance)."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import GraphHarness
from tests.integration.factories import unique

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
V1 = "/api/v1"


class Api:
    """Tiny client wrapper: acts as one user, checks status codes."""

    def __init__(self, client: AsyncClient, user_id: str | None = None) -> None:
        self.client = client
        self.user_id = user_id

    def as_user(self, user_id: str) -> "Api":
        return Api(self.client, user_id)

    async def call(self, method: str, path: str, *, expect: int, **kwargs: Any) -> Any:
        headers = {} if self.user_id is None else {"X-ContextLedger-User-Id": self.user_id}
        response: Response = await self.client.request(method, V1 + path, headers=headers, **kwargs)
        assert response.status_code == expect, response.text
        return None if response.status_code == 204 else response.json()


async def user(api: Api, name: str) -> str:
    body = await api.call(
        "POST",
        "/users",
        json={"email": f"{unique(name)}@example.com", "display_name": name.title()},
        expect=201,
    )
    return str(body["id"])


async def workspace(db_client: AsyncClient) -> Mapping[str, Any]:
    """Alice (ADMIN) with an organization, a billing source and a credit-limit history."""
    anonymous = Api(db_client)
    alice = anonymous.as_user(await user(anonymous, "alice"))
    org = await alice.call(
        "POST", "/organizations", json={"name": "Acme", "slug": unique("acme")}, expect=201
    )
    base = f"/organizations/{org['id']}"
    source = await alice.call(
        "POST",
        f"{base}/sources",
        json={"name": "billing-db", "source_type": "SYSTEM_OF_RECORD", "default_authority": 90},
        expect=201,
    )
    versions = []
    for hours, value in ((0, 2000), (5, 5000)):
        versions.append(
            await alice.call(
                "POST",
                f"{base}/facts",
                json={
                    "entity_type": "customer",
                    "external_id": "customer-991",
                    "property": "credit_limit",
                    "value": value,
                    "source_id": source["id"],
                    "valid_from": (T0 + timedelta(hours=hours)).isoformat(),
                },
                expect=201,
            )
        )
    return {"anon": anonymous, "alice": alice, "base": base, "source": source, "v": versions}


# --- facts and time --------------------------------------------------------------------


async def test_record_and_query_facts_through_time(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    alice, base = w["alice"], w["base"]
    entity = f"{base}/entities/customer/customer-991"

    now = await alice.call("GET", f"{entity}/facts", expect=200)
    then = await alice.call(
        "GET",
        f"{entity}/facts",
        params={"valid_at": (T0 + timedelta(hours=1)).isoformat()},
        expect=200,
    )
    timeline = await alice.call("GET", f"{entity}/timeline", expect=200)
    changes = await alice.call(
        "GET",
        f"{entity}/changes",
        params={"start": T0.isoformat(), "end": (T0 + timedelta(hours=6)).isoformat()},
        expect=200,
    )
    history = await alice.call("GET", f"{base}/facts/{w['v'][0]['fact_id']}/history", expect=200)
    lineage = await alice.call("GET", f"{base}/fact-versions/{w['v'][0]['id']}/lineage", expect=200)

    assert [f["version"]["value"] for f in now["facts"]] == [5000]
    assert [f["version"]["value"] for f in then["facts"]] == [2000]
    assert [e["version"]["value"] for e in timeline["entries"]] == [2000, 5000]
    assert [(c["before"]["value"], c["after"]["value"]) for c in changes] == [(2000, 5000)]
    assert [v["version"] for v in history] == [1, 2]
    assert [d["id"] for d in lineage["descendants"]] == [w["v"][1]["id"]]
    assert w["v"][1]["supersedes_id"] == w["v"][0]["id"]


async def test_evidence_is_captured_idempotently_and_linked(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    alice, base = w["alice"], w["base"]
    body = {
        "source_id": w["source"]["id"],
        "evidence_type": "DOCUMENT_EXCERPT",
        "excerpt": "Credit limit raised to 5000 USD.",
        "captured_at": T0.isoformat(),
    }

    first = await alice.call("POST", f"{base}/evidence", json=body, expect=201)
    again = await alice.call("POST", f"{base}/evidence", json=body, expect=200)
    link = {"evidence_id": first["evidence"]["id"]}
    attached = await alice.call(
        "POST", f"{base}/fact-versions/{w['v'][1]['id']}/evidence", json=link, expect=200
    )
    provenance = await alice.call(
        "GET", f"{base}/fact-versions/{w['v'][1]['id']}/provenance", expect=200
    )

    assert first["created"] is True
    assert again["created"] is False
    assert again["evidence"]["id"] == first["evidence"]["id"]
    assert attached == {"linked": True}
    assert [e["evidence"]["id"] for e in provenance["evidence"]] == [first["evidence"]["id"]]
    assert provenance["source"]["name"] == "billing-db"


# --- search and decisions ---------------------------------------------------------------


async def test_search_capture_decide_and_read_the_receipt(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    alice, base = w["alice"], w["base"]
    question = {"query": "What is the credit limit of customer-991?"}

    found = await alice.call("POST", f"{base}/search", json=question, expect=200)
    context = await alice.call("POST", f"{base}/context-snapshots", json=question, expect=201)
    current = context["retrieval"]["results"][0]["version"]["id"]
    receipt = await alice.call(
        "POST",
        f"{base}/decisions",
        json={
            "snapshot_id": context["snapshot_id"],
            "action": "credit.approve_increase",
            "outcome": {"approved": True, "new_limit": 7500},
            "relied_on": [current],
            "agent": "rest-client",
        },
        expect=201,
    )
    reread = await alice.call(
        "GET", f"{base}/decisions/{receipt['decision_id']}/receipt", expect=200
    )
    relying = await alice.call("GET", f"{base}/fact-versions/{current}/decisions", expect=200)

    assert found["results"][0]["version"]["value"] == 5000
    assert found["results"][0]["ranking"]["text_rank"] == 1
    assert receipt["integrity_verified"] is True
    assert reread["receipt_sha256"] == receipt["receipt_sha256"]
    assert relying == {"decision_ids": [receipt["decision_id"]]}


# --- errors, permissions, privacy -------------------------------------------------------


async def test_errors_follow_the_problem_contract(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    alice, anon, base = w["alice"], w["anon"], w["base"]
    bob = anon.as_user(await user(anon, "bob"))
    stranger = anon.as_user(str(uuid4()))
    await alice.call(
        "POST", f"{base}/members", json={"user_id": bob.user_id, "role": "VIEWER"}, expect=201
    )
    fact = {
        "entity_type": "customer",
        "external_id": "customer-7",
        "property": "credit_limit",
        "value": 1,
        "source_id": w["source"]["id"],
        "valid_from": T0.isoformat(),
    }

    denied = await bob.call("POST", f"{base}/facts", json=fact, expect=403)
    outsider = await stranger.call("GET", f"{base}/members", expect=403)
    missing = await alice.call("GET", f"{base}/decisions/{uuid4()}/receipt", expect=404)
    naive = await alice.call(
        "POST", f"{base}/facts", json={**fact, "valid_from": "2026-01-15T09:00:00"}, expect=422
    )
    duplicate = await alice.call(
        "POST",
        f"{base}/sources",
        json={"name": "billing-db", "source_type": "API"},
        expect=409,
    )
    no_graph = await alice.call("GET", f"{base}/impact/sources/{w['source']['id']}", expect=503)

    assert denied["code"] == "permission_denied"
    assert outsider["code"] == "permission_denied"
    assert missing["code"] == "not_found"
    assert naive["code"] == "request_invalid"
    assert [e["field"] for e in naive["errors"]] == ["valid_from"]
    assert duplicate["code"] == "conflict"
    assert no_graph["code"] == "service_unavailable"


async def test_other_organizations_are_invisible(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    anon = w["anon"]
    mallory = anon.as_user(await user(anon, "mallory"))
    await mallory.call(
        "POST", "/organizations", json={"name": "Evil", "slug": unique("evil")}, expect=201
    )

    # Mallory is not a member of Alice's organization: same answer as a non-existent one.
    response = await mallory.call(
        "GET", f"{w['base']}/entities/customer/customer-991/facts", expect=403
    )
    ghost = await mallory.call("GET", f"/organizations/{uuid4()}/members", expect=403)

    assert response["detail"] == ghost["detail"]


async def test_viewers_do_not_see_restricted_facts(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    alice, anon, base = w["alice"], w["anon"], w["base"]
    await alice.call(
        "POST",
        f"{base}/facts",
        json={
            "entity_type": "customer",
            "external_id": "customer-991",
            "property": "internal_risk_notes",
            "value": "watch closely",
            "source_id": w["source"]["id"],
            "valid_from": T0.isoformat(),
            "privacy_scope": "RESTRICTED",
        },
        expect=201,
    )
    viewer = anon.as_user(await user(anon, "viewer"))
    await alice.call(
        "POST", f"{base}/members", json={"user_id": viewer.user_id, "role": "VIEWER"}, expect=201
    )

    seen = await viewer.call("GET", f"{base}/entities/customer/customer-991/facts", expect=200)
    admin = await alice.call("GET", f"{base}/entities/customer/customer-991/facts", expect=200)

    assert {f["property"] for f in seen["facts"]} == {"credit_limit"}
    assert seen["withheld_by_privacy_scope"] == 1
    assert admin["withheld_by_privacy_scope"] == 0


async def test_member_management(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    alice, anon, base = w["alice"], w["anon"], w["base"]
    carol = anon.as_user(await user(anon, "carol"))

    await alice.call(
        "POST", f"{base}/members", json={"user_id": carol.user_id, "role": "VIEWER"}, expect=201
    )
    promoted = await alice.call(
        "PATCH", f"{base}/members/{carol.user_id}", json={"role": "ENGINEER"}, expect=200
    )
    last_admin = await alice.call(
        "PATCH", f"{base}/members/{alice.user_id}", json={"role": "VIEWER"}, expect=409
    )
    await alice.call("DELETE", f"{base}/members/{carol.user_id}", expect=204)
    me = await carol.call("GET", "/users/me", expect=200)

    assert promoted["role"] == "ENGINEER"
    assert last_admin["code"] == "invariant_violation"
    assert me["id"] == carol.user_id
    await carol.call("GET", f"{base}/members", expect=403)


# --- provenance graph --------------------------------------------------------------------


@pytest.mark.graph
async def test_impact_over_rest(
    db_client: AsyncClient,
    db_app: FastAPI,
    graph: GraphHarness,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    db_app.state.graph_reader = graph.reader
    w = await workspace(db_client)
    alice, base = w["alice"], w["base"]
    context = await alice.call(
        "POST", f"{base}/context-snapshots", json={"query": "credit limit"}, expect=201
    )
    relied = context["retrieval"]["results"][0]["version"]["id"]
    decision = await alice.call(
        "POST",
        f"{base}/decisions",
        json={
            "snapshot_id": context["snapshot_id"],
            "action": "credit.approve_increase",
            "outcome": {"approved": True},
            "relied_on": [relied],
        },
        expect=201,
    )
    await graph.projector(sessions, UUID(base.rsplit("/", 1)[1])).drain()

    impact = await alice.call("GET", f"{base}/impact/sources/{w['source']['id']}", expect=200)
    lineage = await alice.call(
        "GET", f"{base}/decisions/{decision['decision_id']}/lineage", expect=200
    )

    assert [d["decision_id"] for d in impact["decisions"]] == [decision["decision_id"]]
    assert impact["pending_events"] == 0
    assert lineage["relied_on"][0]["fact_version_id"] == relied
