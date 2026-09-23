"""Embedding providers. OpenAI is exercised through httpx.MockTransport: no network, no cost."""

import json
import math
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.embeddings import EMBEDDING_DIMENSIONS
from app.providers.embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingProviderError,
    OpenAIEmbeddingProvider,
    build_embedding_provider,
)

API_KEY = "sk-test-secret-value"
DIMS = 4


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- deterministic ------------------------------------------------------------------


async def test_deterministic_vectors_are_unit_length_and_full_width() -> None:
    batch = await DeterministicHashEmbeddingProvider().embed(["customer credit limit: 2000"])

    [vector] = batch.vectors
    assert len(vector) == EMBEDDING_DIMENSIONS
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)
    assert batch.total_tokens == 0


async def test_deterministic_embedding_is_repeatable() -> None:
    provider = DeterministicHashEmbeddingProvider()

    first = (await provider.embed(["credit limit 2000"])).vectors[0]
    second = (await provider.embed(["credit limit 2000"])).vectors[0]

    assert first == second


async def test_deterministic_embedding_reflects_word_overlap() -> None:
    vectors = (
        await DeterministicHashEmbeddingProvider().embed(
            ["customer credit limit", "credit limit for customer", "shipping address in berlin"]
        )
    ).vectors

    assert _cosine(vectors[0], vectors[1]) > _cosine(vectors[0], vectors[2])


# --- OpenAI over a mock transport -------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def _provider(
    handler: Handler, sleeps: list[float], *, max_retries: int = 3
) -> OpenAIEmbeddingProvider:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(
        base_url="https://api.openai.test/v1/", transport=httpx.MockTransport(handler)
    )
    return OpenAIEmbeddingProvider(
        client,
        api_key=SecretStr(API_KEY),
        model="text-embedding-3-small",
        dimensions=DIMS,
        max_retries=max_retries,
        sleep=fake_sleep,
    )


def _ok(texts: list[str], *, reverse: bool = False) -> httpx.Response:
    data = [
        {"object": "embedding", "index": i, "embedding": [float(i)] * DIMS}
        for i in range(len(texts))
    ]
    if reverse:
        data.reverse()
    return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 7 * len(texts)}})


async def test_openai_request_and_response_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(json.loads(request.content)["input"], reverse=True)

    batch = await _provider(handler, []).embed(["a", "b", "c"])

    request = seen[0]
    body = json.loads(request.content)
    assert request.url == "https://api.openai.test/v1/embeddings"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert body == {
        "model": "text-embedding-3-small",
        "input": ["a", "b", "c"],
        "dimensions": DIMS,
        "encoding_format": "float",
    }
    assert [v[0] for v in batch.vectors] == [0.0, 1.0, 2.0]  # reordered by "index"
    assert batch.total_tokens == 21


async def test_rate_limit_is_retried_honouring_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            error = {"error": {"message": "slow down"}}
            return httpx.Response(429, headers={"retry-after": "2"}, json=error)
        return _ok(["x"])

    batch = await _provider(handler, sleeps).embed(["x"])

    assert calls == 2
    assert sleeps == [2.0]
    assert len(batch.vectors) == 1


async def test_server_errors_exhaust_retries_with_backoff() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(EmbeddingProviderError, match="after 3 attempts"):
        await _provider(handler, sleeps, max_retries=2).embed(["x"])
    assert len(sleeps) == 2
    assert all(s > 0 for s in sleeps)


async def test_transport_errors_are_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return _ok(["x"])

    assert len((await _provider(handler, []).embed(["x"])).vectors) == 1
    assert calls == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_not_retried(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(EmbeddingProviderError, match=f"HTTP {status}") as info:
        await _provider(handler, []).embed(["x"])
    assert calls == 1
    assert API_KEY not in str(info.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * (DIMS + 1)}]}),
        httpx.Response(200, json={"data": []}),
    ],
)
async def test_malformed_responses_are_rejected(response: httpx.Response) -> None:
    with pytest.raises(EmbeddingProviderError):
        await _provider(lambda _: response, []).embed(["x"])


async def test_empty_input_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert (await _provider(handler, []).embed([])).vectors == []


async def test_oversized_batches_are_rejected_before_sending() -> None:
    provider = _provider(lambda _: _ok(["x"]), [])

    with pytest.raises(EmbeddingProviderError, match="exceeds"):
        await provider.embed(["x"] * (provider.max_batch_size + 1))


def test_model_ids_identify_provider_and_model() -> None:
    assert DeterministicHashEmbeddingProvider().model_id == "deterministic:hash-v1"
    assert _provider(lambda _: _ok(["x"]), []).model_id == "openai:text-embedding-3-small"


def test_factory_defaults_to_the_offline_provider() -> None:
    provider = build_embedding_provider(Settings(_env_file=None))

    assert isinstance(provider, DeterministicHashEmbeddingProvider)


def test_factory_requires_a_client_for_openai() -> None:
    settings = Settings(
        _env_file=None, embedding_provider="openai", openai_api_key=SecretStr(API_KEY)
    )

    with pytest.raises(ValueError, match="httpx"):
        build_embedding_provider(settings)
