"""POST /organizations/{id}/answers end to end (real PostgreSQL, fake model)."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.ai.providers import FakeGenerationProvider, GenerationTimeoutError
from tests.integration.factories import unique

pytestmark = pytest.mark.integration

V1 = "/api/v1"
USER = "X-ContextLedger-User-Id"
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def cite(label: str = "F1", answer: str = "2000 USD") -> str:
    return json.dumps(
        {"answer": answer, "insufficient_evidence": False, "cited_facts": [label], "inferences": []}
    )


async def workspace(client: AsyncClient) -> Mapping[str, Any]:
    owner = (
        await client.post(
            f"{V1}/users", json={"email": f"{unique('o')}@example.com", "display_name": "Owner"}
        )
    ).json()["id"]
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
    versions = []
    for hours, value in ((0, 2000), (5, 5000)):
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
        versions.append(response.json())
    return {"owner": owner, "base": base, "versions": versions}


async def test_a_grounded_answer_with_citations(db_app: FastAPI, db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    model = FakeGenerationProvider(responses=[cite()])
    db_app.state.generation_provider = model

    response = await db_client.post(
        f"{w['base']}/answers",
        headers={USER: w["owner"]},
        json={
            "question": "credit limit of customer-991",
            "valid_at": (T0 + timedelta(hours=1)).isoformat(),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "answered" and body["grounded"] is True
    assert body["answer"] == "2000 USD"
    [citation] = body["citations"]
    assert citation["fact_version_id"] == w["versions"][0]["id"]  # the version valid at 10:00
    assert body["generation"]["prompt_version"] == "grounded-answer-v2"
    assert body["generation"]["provider"] == "fake"
    assert body["retrieval"]["facts_supplied"] == 1
    assert "credit_limit = 5000" not in model.requests[0].messages[-1].content


async def test_generation_failure_is_503_never_an_answer(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    w = await workspace(db_client)
    db_app.state.generation_provider = FakeGenerationProvider(
        responses=[GenerationTimeoutError("slow")]
    )

    response = await db_client.post(
        f"{w['base']}/answers", headers={USER: w["owner"]}, json={"question": "credit limit"}
    )

    assert response.status_code == 503
    assert response.json()["code"] == "generation_unavailable"
    assert "GenerationTimeoutError" in response.json()["detail"]
    assert response.headers["Retry-After"] == "5"
    assert "answer" not in response.json()


async def test_disabled_generation_is_503_after_authorization(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    w = await workspace(db_client)
    db_app.state.generation_provider = None
    outsider = (
        await db_client.post(
            f"{V1}/users", json={"email": f"{unique('x')}@example.com", "display_name": "X"}
        )
    ).json()["id"]

    member = await db_client.post(
        f"{w['base']}/answers", headers={USER: w["owner"]}, json={"question": "limit?"}
    )
    stranger = await db_client.post(
        f"{w['base']}/answers", headers={USER: outsider}, json={"question": "limit?"}
    )

    assert member.status_code == 503 and member.json()["code"] == "service_unavailable"
    assert stranger.status_code == 403  # tenant check first: no information leaks


async def test_nothing_retrievable_means_no_model_call(
    db_app: FastAPI, db_client: AsyncClient
) -> None:
    w = await workspace(db_client)
    model = FakeGenerationProvider()
    db_app.state.generation_provider = model

    response = await db_client.post(
        f"{w['base']}/answers",
        headers={USER: w["owner"]},
        json={"question": "credit limit", "valid_at": (T0 - timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_evidence"
    assert response.json()["generation"] is None
    assert model.requests == []


async def test_invented_citations_are_withheld(db_app: FastAPI, db_client: AsyncClient) -> None:
    w = await workspace(db_client)
    db_app.state.generation_provider = FakeGenerationProvider(responses=[cite("F9", "9999")])

    body = (
        await db_client.post(
            f"{w['base']}/answers", headers={USER: w["owner"]}, json={"question": "credit limit"}
        )
    ).json()

    assert body["status"] == "ungrounded" and body["answer"] is None
    assert body["rejected_citations"] == ["F9"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "x" * 1001},
        {"question": "q", "limit": 50},
        {"question": "q", "valid_at": "2026-01-15T09:00:00"},  # naive datetime
        {"question": "q", "organization_id": "00000000-0000-0000-0000-000000000000"},
    ],
    ids=["missing", "empty", "too-long", "limit", "naive-time", "tenant-in-body"],
)
async def test_invalid_requests_are_rejected(
    db_app: FastAPI, db_client: AsyncClient, payload: dict[str, Any]
) -> None:
    w = await workspace(db_client)
    model = FakeGenerationProvider()
    db_app.state.generation_provider = model

    url = f"{w['base']}/answers"
    response = await db_client.post(url, headers={USER: w["owner"]}, json=payload)

    assert response.status_code == 422
    assert model.requests == []
