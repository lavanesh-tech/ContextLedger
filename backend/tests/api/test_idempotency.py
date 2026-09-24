"""Idempotency-Key middleware on a minimal app (no database needed)."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

from app.api.errors import install_error_handlers
from app.auth.tokens import TokenService
from app.cache.idempotency import IdempotencyMiddleware
from app.cache.store import KeyValueStore, MemoryStore
from app.core.config import Environment, Settings
from app.core.correlation import CorrelationIdMiddleware
from tests.broken_store import BrokenStore

USER = "X-ContextLedger-User-Id"
ALICE = UUID(int=1)
TOKENS = TokenService.from_settings(Settings(_env_file=None, environment=Environment.TEST))


class Harness:
    def __init__(self, store: KeyValueStore) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        app = FastAPI()
        install_error_handlers(app)

        @app.post("/things", status_code=201)
        async def create(request: Request, response: Response) -> dict[str, Any]:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            body = await request.json()
            if body.get("fail"):
                response.status_code = 422
                return {"error": "rejected"}
            response.headers["Location"] = f"/things/{self.calls}"
            response.headers["X-Not-Replayed"] = "1"
            return {"id": str(uuid4()), "call": self.calls, **body}

        @app.post("/oauth/token")
        async def token() -> dict[str, int]:
            self.calls += 1
            return {"call": self.calls}

        app.add_middleware(
            IdempotencyMiddleware,
            store=store,
            tokens=TOKENS,
            allow_header_auth=True,
            ttl_seconds=3600,
            lock_seconds=30,
        )
        app.add_middleware(CorrelationIdMiddleware, header_name="X-Correlation-ID")
        self.app = app


@pytest.fixture
async def harness() -> AsyncIterator[tuple[Harness, AsyncClient]]:
    h = Harness(MemoryStore())
    async with AsyncClient(transport=ASGITransport(app=h.app), base_url="http://t") as client:
        yield h, client


def headers(key: str | None, user: UUID = ALICE) -> dict[str, str]:
    result = {USER: str(user)}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


async def test_a_retry_replays_the_first_response(harness: tuple[Harness, AsyncClient]) -> None:
    h, client = harness
    first = await client.post("/things", json={"n": 1}, headers=headers("k-1"))
    retry = await client.post("/things", json={"n": 1}, headers=headers("k-1"))

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert h.calls == 1
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.headers["Location"] == first.headers["Location"]
    assert "X-Not-Replayed" not in retry.headers
    assert "Idempotent-Replayed" not in first.headers
    assert retry.headers["X-Correlation-ID"]


async def test_without_a_key_every_request_runs(harness: tuple[Harness, AsyncClient]) -> None:
    h, client = harness
    await client.post("/things", json={"n": 1}, headers=headers(None))
    await client.post("/things", json={"n": 1}, headers=headers(None))
    assert h.calls == 2


async def test_reusing_a_key_for_a_different_request_is_rejected(
    harness: tuple[Harness, AsyncClient],
) -> None:
    h, client = harness
    await client.post("/things", json={"n": 1}, headers=headers("k-2"))
    other = await client.post("/things", json={"n": 2}, headers=headers("k-2"))

    assert other.status_code == 422
    assert other.json()["code"] == "idempotency_key_reused"
    assert other.headers["content-type"] == "application/problem+json"
    assert h.calls == 1


async def test_keys_belong_to_one_caller(harness: tuple[Harness, AsyncClient]) -> None:
    h, client = harness
    await client.post("/things", json={"n": 1}, headers=headers("shared"))
    other = await client.post("/things", json={"n": 1}, headers=headers("shared", UUID(int=2)))

    assert other.status_code == 201
    assert "Idempotent-Replayed" not in other.headers
    assert h.calls == 2


async def test_bearer_callers_are_scoped_by_verified_identity(
    harness: tuple[Harness, AsyncClient],
) -> None:
    h, client = harness
    user = uuid4()
    first_token, _ = TOKENS.issue(subject=f"user:{user}", user_id=user)
    second_token, _ = TOKENS.issue(subject=f"user:{user}", user_id=user)  # e.g. refreshed

    await client.post(
        "/things",
        json={"n": 1},
        headers={"Authorization": f"Bearer {first_token}", "Idempotency-Key": "k"},
    )
    retry = await client.post(
        "/things",
        json={"n": 1},
        headers={"Authorization": f"Bearer {second_token}", "Idempotency-Key": "k"},
    )

    assert retry.headers["Idempotent-Replayed"] == "true"
    assert h.calls == 1


async def test_failed_requests_release_the_key(harness: tuple[Harness, AsyncClient]) -> None:
    h, client = harness
    failed = await client.post("/things", json={"fail": True}, headers=headers("k-3"))
    again = await client.post("/things", json={"fail": True}, headers=headers("k-3"))

    assert failed.status_code == again.status_code == 422
    assert "Idempotent-Replayed" not in again.headers
    assert h.calls == 2


async def test_a_retry_while_the_first_is_running_gets_409(
    harness: tuple[Harness, AsyncClient],
) -> None:
    h, client = harness
    h.release.clear()
    first = asyncio.create_task(client.post("/things", json={"n": 1}, headers=headers("k-4")))
    await h.started.wait()

    concurrent = await client.post("/things", json={"n": 1}, headers=headers("k-4"))
    h.release.set()
    completed = await first

    assert concurrent.status_code == 409
    assert concurrent.json()["code"] == "idempotency_key_in_use"
    assert concurrent.headers["Retry-After"] == "1"
    assert completed.status_code == 201
    assert h.calls == 1


@pytest.mark.parametrize("key", ["", "has space", "x" * 256])
async def test_malformed_keys_are_rejected(harness: tuple[Harness, AsyncClient], key: str) -> None:
    h, client = harness
    response = await client.post(
        "/things", json={}, headers={USER: str(ALICE), "Idempotency-Key": key}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_invalid"
    assert h.calls == 0


async def test_unauthenticated_requests_pass_through(harness: tuple[Harness, AsyncClient]) -> None:
    h, client = harness
    for token in ("Bearer not-a-jwt", "Basic abc"):
        await client.post(
            "/things", json={}, headers={"Authorization": token, "Idempotency-Key": "k"}
        )
    assert h.calls == 2  # the real endpoints answer 401; nothing is reserved


async def test_the_token_endpoint_is_never_recorded(harness: tuple[Harness, AsyncClient]) -> None:
    h, client = harness
    await client.post("/oauth/token", headers=headers("k-5"))
    await client.post("/oauth/token", headers=headers("k-5"))
    assert h.calls == 2


async def test_an_unavailable_store_fails_closed() -> None:
    h = Harness(BrokenStore())
    async with AsyncClient(transport=ASGITransport(app=h.app), base_url="http://t") as client:
        with_key = await client.post("/things", json={}, headers=headers("k"))
        without_key = await client.post("/things", json={}, headers=headers(None))

    assert with_key.status_code == 503
    assert with_key.json()["code"] == "service_unavailable"
    assert without_key.status_code == 201
    assert h.calls == 1
