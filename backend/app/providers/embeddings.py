"""Embedding providers.

``EmbeddingProvider`` is the only thing the worker depends on, so providers are
interchangeable and tests never call a real API:

* ``OpenAIEmbeddingProvider``: OpenAI ``/v1/embeddings`` over httpx, with
  bounded retries (429/5xx/timeouts, honouring ``Retry-After``) and strict
  response validation (count, order, dimensions).
* ``DeterministicHashEmbeddingProvider``: offline feature-hashing embedder. It
  captures word overlap, not meaning, and is labelled as such everywhere it is used.
"""

import asyncio
import hashlib
import math
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final, Protocol

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.embeddings import EMBEDDING_DIMENSIONS

_RETRYABLE_STATUS: Final = frozenset({408, 409, 429, 500, 502, 503, 504})
_TOKEN: Final = re.compile(r"[a-z0-9]+")


class EmbeddingProviderError(Exception):
    """A provider call failed. Messages never contain credentials."""


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    total_tokens: int


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str:
        """Stable identifier stored with every embedding, e.g. 'openai:text-embedding-3-small'."""
        ...

    @property
    def max_batch_size(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch: ...


class DeterministicHashEmbeddingProvider:
    """Signed feature hashing of lower-cased word unigrams and bigrams, L2-normalised."""

    max_batch_size = 512

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return "deterministic:hash-v1"

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        return EmbeddingBatch(vectors=[self._embed_one(t) for t in texts], total_tokens=0)

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        tokens = _TOKEN.findall(text.lower())
        features = tokens + [f"{a} {b}" for a, b in pairwise(tokens)]
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]


Sleep = Callable[[float], Awaitable[None]]


class OpenAIEmbeddingProvider:
    max_batch_size = 256  # OpenAI accepts more; smaller batches bound retry cost

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: SecretStr,
        model: str,
        dimensions: int = EMBEDDING_DIMENSIONS,
        max_retries: int = 3,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 20.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._max_retries = max_retries
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._sleep = sleep

    @property
    def model_id(self) -> str:
        return f"openai:{self._model}"

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(vectors=[], total_tokens=0)
        if len(texts) > self.max_batch_size:
            raise EmbeddingProviderError(f"batch of {len(texts)} exceeds {self.max_batch_size}")
        payload = {
            "model": self._model,
            "input": list(texts),
            "dimensions": self._dimensions,
            "encoding_format": "float",
        }
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}

        last_problem = "no attempt made"
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                response = await self._client.post("embeddings", json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_problem = f"transport error: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return self._parse(response, expected=len(texts))
                last_problem = f"HTTP {response.status_code}: {_error_message(response)}"
                if response.status_code not in _RETRYABLE_STATUS:
                    raise EmbeddingProviderError(
                        f"OpenAI embeddings request failed ({last_problem})"
                    )
                retry_after = _retry_after_seconds(response)
            if attempt < self._max_retries:
                await self._sleep(self._delay(attempt, retry_after))
        raise EmbeddingProviderError(
            f"OpenAI embeddings failed after {self._max_retries + 1} attempts ({last_problem})"
        )

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._max_delay)
        backoff = self._base_delay * 2**attempt
        return float(min(self._max_delay, backoff * random.uniform(0.5, 1.0)))  # noqa: S311

    def _parse(self, response: httpx.Response, *, expected: int) -> EmbeddingBatch:
        try:
            body: dict[str, Any] = response.json()
            items = sorted(body["data"], key=lambda item: int(item["index"]))
            vectors = [[float(x) for x in item["embedding"]] for item in items]
            tokens = int(body.get("usage", {}).get("total_tokens", 0))
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingProviderError("OpenAI returned a malformed embeddings response") from exc
        if len(vectors) != expected:
            raise EmbeddingProviderError(f"expected {expected} embeddings, got {len(vectors)}")
        if any(len(v) != self._dimensions for v in vectors):
            raise EmbeddingProviderError(f"expected {self._dimensions}-dimensional embeddings")
        return EmbeddingBatch(vectors=vectors, total_tokens=tokens)


def _error_message(response: httpx.Response) -> str:
    try:
        message = str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        message = response.reason_phrase
    return message[:200]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def build_embedding_provider(
    settings: Settings, http_client: httpx.AsyncClient | None = None
) -> EmbeddingProvider:
    """Provider selected by settings. The caller owns (and closes) ``http_client``."""
    if settings.embedding_provider == "deterministic":
        return DeterministicHashEmbeddingProvider()
    if http_client is None:
        raise ValueError("the openai provider needs an httpx.AsyncClient")
    return OpenAIEmbeddingProvider(
        http_client,
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        max_retries=settings.openai_max_retries,
    )


def build_openai_http_client(settings: Settings) -> httpx.AsyncClient:
    base_url = str(settings.openai_base_url).rstrip("/") + "/"
    return httpx.AsyncClient(base_url=base_url, timeout=settings.openai_timeout_seconds)
