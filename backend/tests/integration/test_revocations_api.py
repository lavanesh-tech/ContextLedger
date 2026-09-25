"""Revocation impact end to end: FactVersion → ContextSnapshot → Decision → impact."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.factories import unique

pytestmark = pytest.mark.integration

V1 = "/api/v1"
USER = "X-ContextLedger-User-Id"
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


async def new_user(client: AsyncClient, name: str) -> str:
    response = await client.post(
        f"{V1}/users", json={"email": f"{unique(name)}@example.com", "display_name": name}
    )
    return str(response.json()["id"])


async def workspace(client: AsyncClient) -> Mapping[str, Any]:
    """credit_limit and risk_score for one customer, two decisions made from one context:
    the first relied on the credit limit, the second only on the risk score."""
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
    versions = {}
    for prop, value in (("credit_limit", 5000), ("risk_score", 12)):
        response = await client.post(
            f"{base}/facts",
            headers={USER: owner},
            json={
                "entity_type": "customer",
                "external_id": "customer-991",
                "property": prop,
                "value": value,
                "source_id": source,
                "valid_from": T0.isoformat(),
            },
        )
        assert response.status_code == 201, response.text
        versions[prop] = response.json()
    snapshot = await client.post(
        f"{base}/context-snapshots",
        headers={USER: owner},
        json={"query": "customer-991 credit_limit risk_score", "limit": 5},
    )
    assert snapshot.status_code == 201, snapshot.text
    in_context = {f["version"]["id"] for f in snapshot.json()["retrieval"]["results"]}
    assert in_context == {v["id"] for v in versions.values()}
    decisions = []
    for action, relied in (
        ("credit.approve_increase", versions["credit_limit"]["id"]),
        ("risk.flag_review", versions["risk_score"]["id"]),
    ):
        decision = await client.post(
            f"{base}/decisions",
            headers={USER: owner},
            json={
                "snapshot_id": snapshot.json()["snapshot_id"],
                "action": action,
                "outcome": {"ok": True},
                "relied_on": [relied],
            },
        )
        assert decision.status_code == 201, decision.text
        decisions.append(decision.json()["decision_id"])
    return {
        "owner": owner,
        "org": org,
        "base": base,
        "limit": versions["credit_limit"],
        "decisions": decisions,
        "snapshot_id": snapshot.json()["snapshot_id"],
    }


async def revoke(client: AsyncClient, w: Mapping[str, Any], user: str | None = None) -> Any:
    return await client.post(
        f"{w['base']}/facts/{w['limit']['fact_id']}/revoke",
        headers={USER: user or w["owner"]},
        json={"reason": "Imported in the wrong currency."},
    )


async def test_revocation_lists_every_decision_that_had_the_version(
    db_client: AsyncClient, engine: AsyncEngine
) -> None:
    w = await workspace(db_client)

    response = await revoke(db_client, w)

    assert response.status_code == 201, response.text
    report = response.json()
    assert report["version"]["id"] == w["limit"]["id"] and report["property"] == "credit_limit"
    assert [(d["decision_id"], d["relied_on"]) for d in report["decisions"]] == [
        (w["decisions"][0], True),
        (w["decisions"][1], False),  # in the context, but not cited
    ]
    assert report["decisions_relied_on"] == 1
    assert report["decisions_with_version_in_context"] == 2
    assert all(d["recorded_at_revocation"] for d in report["decisions"])

    async with engine.connect() as connection:
        types = (
            (
                await connection.execute(
                    text(
                        "SELECT event_type FROM event_outbox WHERE organization_id = :org "
                        "AND event_type IN ('fact.revoked', 'decision.impacted') ORDER BY id"
                    ),
                    {"org": w["org"]},
                )
            )
            .scalars()
            .all()
        )
    assert types == ["fact.revoked", "decision.impacted", "decision.impacted"]


async def test_a_revoked_version_leaves_current_answers_but_not_the_past(
    db_client: AsyncClient,
) -> None:
    w = await workspace(db_client)
    facts_url = f"{w['base']}/entities/customer/customer-991/facts"
    before = datetime.now(UTC)

    assert (await revoke(db_client, w)).status_code == 201

    now = await db_client.get(facts_url, headers={USER: w["owner"]})
    then = await db_client.get(
        facts_url, headers={USER: w["owner"]}, params={"known_at": before.isoformat()}
    )
    search = await db_client.post(
        f"{w['base']}/search", headers={USER: w["owner"]}, json={"query": "credit_limit"}
    )
    assert {f["property"] for f in now.json()["facts"]} == {"risk_score"}
    assert {f["property"] for f in then.json()["facts"]} == {"credit_limit", "risk_score"}
    assert w["limit"]["id"] not in {r["version"]["id"] for r in search.json()["results"]}
    history = await db_client.get(
        f"{w['base']}/facts/{w['limit']['fact_id']}/history", headers={USER: w["owner"]}
    )
    assert [v["id"] for v in history.json()] == [w["limit"]["id"]]  # never deleted


async def test_receipts_still_show_the_version_and_mark_it_revoked(
    db_client: AsyncClient,
) -> None:
    w = await workspace(db_client)
    await revoke(db_client, w)

    receipt = await db_client.get(
        f"{w['base']}/decisions/{w['decisions'][0]}/receipt", headers={USER: w["owner"]}
    )

    body = receipt.json()
    assert body["integrity_verified"] is True  # the sealed receipt is unchanged
    limit = next(f for f in body["facts"] if f["fact_version_id"] == w["limit"]["id"])
    assert limit["version"]["value"] == 5000 and limit["revoked_at"] is not None


async def test_a_version_is_revoked_once_and_the_audit_is_immutable(
    db_client: AsyncClient, engine: AsyncEngine
) -> None:
    w = await workspace(db_client)
    first = await revoke(db_client, w)
    again = await revoke(db_client, w)

    assert again.status_code == 409
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE fact_revocations SET reason = 'x' WHERE id = :id"),
                {"id": first.json()["revocation_id"]},
            )


async def test_a_decision_recorded_after_the_revocation_is_still_reported(
    db_client: AsyncClient,
) -> None:
    w = await workspace(db_client)
    await revoke(db_client, w)
    late = await db_client.post(
        f"{w['base']}/decisions",
        headers={USER: w["owner"]},
        json={
            "snapshot_id": w["snapshot_id"],
            "action": "credit.renew",
            "outcome": {"ok": True},
            "relied_on": [w["limit"]["id"]],
        },
    )
    assert late.status_code == 201, late.text

    [report] = (
        await db_client.get(
            f"{w['base']}/facts/{w['limit']['fact_id']}/impact", headers={USER: w["owner"]}
        )
    ).json()

    newest = report["decisions"][-1]
    assert newest["decision_id"] == late.json()["decision_id"]
    assert newest["decided_after_revocation"] is True and newest["relied_on"] is True


async def test_viewers_cannot_revoke_and_other_versions_are_not_found(
    db_client: AsyncClient,
) -> None:
    w = await workspace(db_client)
    viewer = await new_user(db_client, "viewer")
    await db_client.post(
        f"{w['base']}/members",
        headers={USER: w["owner"]},
        json={"user_id": viewer, "role": "VIEWER"},
    )
    other = await workspace(db_client)

    forbidden = await revoke(db_client, w, viewer)
    foreign_version = await db_client.post(
        f"{w['base']}/facts/{w['limit']['fact_id']}/revoke",
        headers={USER: w["owner"]},
        json={"reason": "x", "version_id": other["limit"]["id"]},
    )
    listed = await db_client.get(f"{w['base']}/revocations", headers={USER: w["owner"]})

    assert forbidden.status_code == 403
    assert foreign_version.status_code == 404
    assert listed.json() == []
