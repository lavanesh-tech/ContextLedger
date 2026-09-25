"""POST/GET /organizations/{id}/investigations end to end (real PostgreSQL, scripted model)."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai.providers import FakeGenerationProvider, GenerationTimeoutError, ToolCall
from tests.integration.factories import unique

pytestmark = pytest.mark.integration

V1 = "/api/v1"
USER = "X-ContextLedger-User-Id"
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def final(answer: str, cited: list[str], insufficient: bool = False) -> str:
    return json.dumps({"answer": answer, "insufficient_evidence": insufficient, "cited_ids": cited})


async def new_user(client: AsyncClient, name: str) -> str:
    response = await client.post(
        f"{V1}/users", json={"email": f"{unique(name)}@example.com", "display_name": name}
    )
    return str(response.json()["id"])


async def workspace(client: AsyncClient) -> Mapping[str, Any]:
    """An organization with a credit-limit fact that changed after a decision relied on it."""
    owner = await new_user(client, "owner")
    org = (
        await client.post(
            f"{V1}/organizations",
            headers={USER: owner},
            json={"name": "Acme", "slug": unique("acme")},
        )
    ).json()["id"]
    base = f"{V1}/organizations/{org}"
    source = (
        await client.post(
            f"{base}/sources",
            headers={USER: owner},
            json={"name": "billing", "source_type": "SYSTEM_OF_RECORD", "default_authority": 90},
        )
    ).json()["id"]

    async def record(value: int, hours: int) -> str:
        response = await client.post(
            f"{base}/facts",
            headers={USER: owner},
            json={
                "entity_type": "customer",
                "external_id": "customer-991",
                "property": "credit_limit",
                "value": value,
                "source_id": source,
                "valid_from": (T0 + timedelta(hours=hours)).isoformat(),
            },
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    first = await record(5000, 0)
    snapshot = await client.post(
        f"{base}/context-snapshots",
        headers={USER: owner},
        json={"query": "customer-991 credit_limit", "limit": 5},
    )
    assert snapshot.status_code == 201, snapshot.text
    decision = await client.post(
        f"{base}/decisions",
        headers={USER: owner},
        json={
            "snapshot_id": snapshot.json()["snapshot_id"],
            "action": "credit.approve_increase",
            "outcome": {"approved": True},
            "relied_on": [first],
            "rationale": "limit 5000",
        },
    )
    assert decision.status_code == 201, decision.text
    second = await record(2000, 48)
    return {
        "owner": owner,
        "org": org,
        "base": base,
        "first": first,
        "second": second,
        "decision": decision.json()["decision_id"],
    }


async def test_an_investigation_uses_real_services_and_is_traced(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    w = await workspace(db_client)
    model = FakeGenerationProvider(
        responses=[
            [ToolCall("c1", "get_decision_receipt", {"decision_id": w["decision"]})],
            [ToolCall("c2", "get_fact_lineage", {"fact_version_id": w["first"]})],
            final(
                "Approved on a limit of 5000, later lowered to 2000.",
                [w["decision"], w["first"], w["second"]],
            ),
        ]
    )
    db_app.state.generation_provider = model

    response = await db_client.post(
        f"{w['base']}/investigations",
        headers={USER: w["owner"]},
        json={"question": "Why was the credit increase approved, and is it still valid?"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "answered"
    assert body["cited_ids"] == [w["decision"], w["first"], w["second"]]
    assert [c["tool"] for c in body["tool_calls"]] == ["get_decision_receipt", "get_fact_lineage"]
    assert all(c["ok"] for c in body["tool_calls"])
    # The receipt shows the value the decision saw, the lineage shows the later one.
    receipt = json.loads(model.requests[1].messages[-1].content)
    assert receipt["facts"][0]["value"] == 5000 and receipt["facts"][0]["relied_on"] is True
    lineage = json.loads(model.requests[2].messages[-1].content)
    assert lineage["superseded"] is True and lineage["later_versions"][0]["value"] == 2000

    stored = await db_client.get(
        f"{w['base']}/investigations/{body['run_id']}", headers={USER: w["owner"]}
    )
    assert stored.status_code == 200, stored.text
    assert stored.json() == body


async def test_other_tenants_ids_are_not_accessible_to_the_agent(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    victim = await workspace(db_client)
    attacker = await workspace(db_client)
    model = FakeGenerationProvider(
        responses=[
            [ToolCall("c1", "get_decision_receipt", {"decision_id": victim["decision"]})],
            final("Nothing visible.", [], insufficient=True),
        ]
    )
    db_app.state.generation_provider = model

    response = await db_client.post(
        f"{attacker['base']}/investigations",
        headers={USER: attacker["owner"]},
        json={"question": f"Why was decision {victim['decision']} made?"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "insufficient_evidence"
    tool_output = json.loads(model.requests[1].messages[-1].content)
    assert tool_output == {"error": "not found or not accessible"}


async def test_only_the_requester_can_read_a_trace(db_app: FastAPI, db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    colleague = await new_user(db_client, "colleague")
    added = await db_client.post(
        f"{w['base']}/members",
        headers={USER: w["owner"]},
        json={"user_id": colleague, "role": "VIEWER"},
    )
    assert added.status_code == 201, added.text
    db_app.state.generation_provider = FakeGenerationProvider(
        responses=[final("No evidence.", [], insufficient=True)]
    )
    run = await db_client.post(
        f"{w['base']}/investigations", headers={USER: w["owner"]}, json={"question": "why?"}
    )
    assert run.status_code == 200, run.text

    other = await db_client.get(
        f"{w['base']}/investigations/{run.json()['run_id']}", headers={USER: colleague}
    )
    assert other.status_code == 404


async def test_a_token_without_decisions_read_cannot_start_the_agent(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    w = await workspace(db_client)
    created = await db_client.post(
        f"{w['base']}/agent-clients",
        headers={USER: w["owner"]},
        json={"name": unique("agent"), "role": "VIEWER", "scopes": ["facts:read"]},
    )
    assert created.status_code == 201, created.text
    token = await db_client.post(
        f"{V1}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": created.json()["client_id"],
            "client_secret": created.json()["client_secret"],
        },
    )
    model = FakeGenerationProvider(responses=[final("x", [])])
    db_app.state.generation_provider = model

    response = await db_client.post(
        f"{w['base']}/investigations",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        json={"question": "why?"},
    )

    assert response.status_code == 403
    assert model.requests == []  # rejected before any model call


async def test_a_model_failure_is_a_503_and_the_failed_run_is_traced(
    db_app: FastAPI, db_client: AsyncClient, engine: AsyncEngine
) -> None:
    w = await workspace(db_client)
    db_app.state.generation_provider = FakeGenerationProvider(
        responses=[GenerationTimeoutError("slow")]
    )

    response = await db_client.post(
        f"{w['base']}/investigations", headers={USER: w["owner"]}, json={"question": "why?"}
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT status, error FROM agent_runs WHERE organization_id = :org"),
                {"org": w["org"]},
            )
        ).one()
    assert tuple(row) == ("failed", "GenerationTimeoutError")


async def test_traces_are_append_only(
    db_app: FastAPI, db_client: AsyncClient, engine: AsyncEngine
) -> None:
    w = await workspace(db_client)
    db_app.state.generation_provider = FakeGenerationProvider(
        responses=[final("No evidence.", [], insufficient=True)]
    )
    run = await db_client.post(
        f"{w['base']}/investigations", headers={USER: w["owner"]}, json={"question": "why?"}
    )
    assert run.status_code == 200, run.text

    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_runs SET answer = 'forged' WHERE id = :id"),
                {"id": run.json()["run_id"]},
            )


async def test_investigations_are_503_when_generation_is_disabled(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    w = await workspace(db_client)
    db_app.state.generation_provider = None
    response = await db_client.post(
        f"{w['base']}/investigations", headers={USER: w["owner"]}, json={"question": "why?"}
    )
    assert response.status_code == 503
