"""Contradictions end to end: detection on write, visibility, resolution, events, LLM review."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai.providers import FakeGenerationProvider
from tests.integration.factories import unique

pytestmark = pytest.mark.integration

V1 = "/api/v1"
USER = "X-ContextLedger-User-Id"
T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


async def new_user(client: AsyncClient, name: str) -> str:
    response = await client.post(
        f"{V1}/users", json={"email": f"{unique(name)}@example.com", "display_name": name}
    )
    return str(response.json()["id"])


async def workspace(client: AsyncClient) -> Mapping[str, Any]:
    owner = await new_user(client, "owner")
    org = (
        await client.post(
            f"{V1}/organizations",
            headers={USER: owner},
            json={"name": "Acme", "slug": unique("acme")},
        )
    ).json()["id"]
    base = f"{V1}/organizations/{org}"
    sources = {}
    for name, authority in (("crm", 60), ("billing", 90)):
        response = await client.post(
            f"{base}/sources",
            headers={USER: owner},
            json={"name": name, "source_type": "SYSTEM_OF_RECORD", "default_authority": authority},
        )
        sources[name] = response.json()["id"]
    return {"owner": owner, "org": org, "base": base, "sources": sources}


async def record(
    client: AsyncClient,
    w: Mapping[str, Any],
    source: str,
    value: Any,
    *,
    hours: int,
    observed: int | None = None,
    prop: str = "customer_status",
    scope: str = "INTERNAL",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "entity_type": "customer",
        "external_id": "customer-991",
        "property": prop,
        "value": value,
        "source_id": w["sources"][source],
        "valid_from": (T0 + timedelta(hours=hours)).isoformat(),
        "privacy_scope": scope,
    }
    if observed is not None:
        body["observed_at"] = (T0 + timedelta(hours=observed)).isoformat()
    response = await client.post(f"{w['base']}/facts", headers={USER: w["owner"]}, json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def contradictions(client: AsyncClient, w: Mapping[str, Any], user: str) -> list[Any]:
    response = await client.get(f"{w['base']}/contradictions", headers={USER: user})
    assert response.status_code == 200, response.text
    return list(response.json())


async def test_a_conflicting_source_is_detected_and_both_versions_are_kept(
    db_client: AsyncClient, engine: AsyncEngine
) -> None:
    w = await workspace(db_client)
    crm = await record(db_client, w, "crm", "ACTIVE", hours=0, observed=4)
    billing = await record(db_client, w, "billing", "SUSPENDED", hours=2)

    [found] = await contradictions(db_client, w, w["owner"])

    assert found["kind"] == "value_conflict" and found["status"] == "open"
    assert found["detector"] == "observed-value-conflict-v1"
    assert (found["left"]["version"]["id"], found["right"]["version"]["id"]) == (
        crm["id"],
        billing["id"],
    )
    assert (found["left"]["source_name"], found["right"]["source_name"]) == ("crm", "billing")
    assert found["preferred_version_id"] == billing["id"]  # authority 90 > 60
    history = await db_client.get(
        f"{w['base']}/facts/{crm['fact_id']}/history", headers={USER: w["owner"]}
    )
    assert len(history.json()) == 2  # nothing discarded
    async with engine.connect() as connection:
        events = (
            await connection.execute(
                text(
                    "SELECT event_type FROM event_outbox WHERE organization_id = :org "
                    "AND event_type = 'contradiction.detected'"
                ),
                {"org": w["org"]},
            )
        ).all()
    assert len(events) == 1


async def test_ordinary_updates_and_same_source_corrections_are_not_flagged(
    db_client: AsyncClient,
) -> None:
    w = await workspace(db_client)
    await record(db_client, w, "crm", "ACTIVE", hours=0)
    await record(db_client, w, "billing", "SUSPENDED", hours=2)
    await record(db_client, w, "billing", "ACTIVE", hours=4, observed=8, prop="tier")
    await record(db_client, w, "billing", "GOLD", hours=6, prop="tier")

    assert await contradictions(db_client, w, w["owner"]) == []


async def test_contradictions_above_the_readers_ceiling_are_hidden(
    db_client: AsyncClient,
) -> None:
    w = await workspace(db_client)
    await record(db_client, w, "crm", "ACTIVE", hours=0, observed=4, scope="CONFIDENTIAL")
    await record(db_client, w, "billing", "SUSPENDED", hours=2)
    viewer = await new_user(db_client, "viewer")
    added = await db_client.post(
        f"{w['base']}/members",
        headers={USER: w["owner"]},
        json={"user_id": viewer, "role": "VIEWER"},
    )
    assert added.status_code == 201, added.text

    [visible] = await contradictions(db_client, w, w["owner"])
    assert visible["privacy_scope"] == "CONFIDENTIAL"
    assert await contradictions(db_client, w, viewer) == []
    hidden = await db_client.get(
        f"{w['base']}/contradictions/{visible['id']}", headers={USER: viewer}
    )
    assert hidden.status_code == 404


async def test_resolving_records_who_and_why_once(db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    await record(db_client, w, "crm", "ACTIVE", hours=0, observed=4)
    await record(db_client, w, "billing", "SUSPENDED", hours=2)
    [found] = await contradictions(db_client, w, w["owner"])
    viewer = await new_user(db_client, "viewer")
    await db_client.post(
        f"{w['base']}/members",
        headers={USER: w["owner"]},
        json={"user_id": viewer, "role": "VIEWER"},
    )
    url = f"{w['base']}/contradictions/{found['id']}"

    forbidden = await db_client.patch(url, headers={USER: viewer}, json={"status": "resolved"})
    resolved = await db_client.patch(
        url, headers={USER: w["owner"]}, json={"status": "resolved", "note": "billing is right"}
    )
    again = await db_client.patch(url, headers={USER: w["owner"]}, json={"status": "dismissed"})
    reopen = await db_client.patch(url, headers={USER: w["owner"]}, json={"status": "open"})
    open_only = await db_client.get(
        f"{w['base']}/contradictions", headers={USER: w["owner"]}, params={"status": "open"}
    )

    assert forbidden.status_code == 403
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "resolved" and body["resolution_note"] == "billing is right"
    assert body["resolved_by_user_id"] == w["owner"] and body["resolved_at"] is not None
    assert again.status_code == 409
    assert reopen.status_code == 422
    assert open_only.json() == []


def review_model(findings: list[dict[str, str]]) -> FakeGenerationProvider:
    return FakeGenerationProvider(responder=lambda _: json.dumps({"findings": findings}))


async def test_llm_review_suggestions_are_validated_and_stored_once(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    w = await workspace(db_client)
    closed = await record(db_client, w, "billing", True, hours=0, prop="account_closed")
    status = await record(db_client, w, "crm", "ACTIVE", hours=0)
    model = review_model(
        [
            {"left": "F1", "right": "F2", "explanation": "A closed account cannot be ACTIVE."},
            {"left": "F1", "right": "F9", "explanation": "invented label"},
            {"left": "F2", "right": "F2", "explanation": "same fact"},
        ]
    )
    db_app.state.generation_provider = model
    url = f"{w['base']}/entities/customer/customer-991/contradiction-review"

    first = await db_client.post(url, headers={USER: w["owner"]})
    second = await db_client.post(url, headers={USER: w["owner"]})

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["facts_reviewed"] == 2 and body["rejected_findings"] == 2
    [created] = body["created"]
    assert created["kind"] == "semantic" and created["detector"] == "llm:contradiction-review-v1"
    assert created["preferred_version_id"] is None
    assert {created["left"]["version"]["id"], created["right"]["version"]["id"]} == {
        closed["id"],
        status["id"],
    }
    prompt = model.requests[0].messages[-1].content
    assert "[F1] account_closed = true" in prompt and "[F2] customer_status" in prompt
    assert second.json()["created"] == [] and second.json()["already_known"] == 1


async def test_llm_review_needs_generation(db_app: FastAPI, db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    db_app.state.generation_provider = None
    response = await db_client.post(
        f"{w['base']}/entities/customer/customer-991/contradiction-review",
        headers={USER: w["owner"]},
    )
    assert response.status_code == 503
