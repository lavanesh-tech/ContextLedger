"""Text generation providers (LLM calls), behind one small interface.

``GenerationProvider`` is what the grounded-answer service and the agent depend
on. It knows nothing about tenants, retrieval, prompts or LangChain: it turns a
list of messages into text, with usage metadata, or raises a typed error.

* ``OpenAIChatProvider``: OpenAI Chat Completions over httpx (the same client
  and retry conventions as ``OpenAIEmbeddingProvider``). Optional strict JSON
  Schema output. Bounded retries for 408/409/429/5xx and transport errors,
  honouring ``Retry-After``; an overall deadline per call; no retry for
  authentication or request errors.
* ``FakeGenerationProvider``: scripted, deterministic responses for tests and
  offline evaluation. It records every request it receives, so tests can assert
  exactly what would have been sent to a model.

Failures are never turned into answers: every error path raises
``GenerationError``. Cancellation (``asyncio.CancelledError``) is never caught.
"""

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

import httpx
from pydantic import SecretStr

from app.core.config import Settings

Role = Literal["system", "user", "assistant"]
_RETRYABLE_STATUS: Final = frozenset({408, 409, 429, 500, 502, 503, 504})


# --- errors -------------------------------------------------------------------------


class GenerationError(Exception):
    """A generation call failed. Messages never contain credentials or prompt content."""

    retryable: bool = False


class GenerationTimeoutError(GenerationError):
    retryable = True


class GenerationRateLimitedError(GenerationError):
    retryable = True


class GenerationUnavailableError(GenerationError):
    """Transport failure or a 5xx after all retries."""

    retryable = True


class GenerationAuthenticationError(GenerationError):
    """401/403: the key is missing, wrong or lacks access. Never retried."""


class GenerationRequestError(GenerationError):
    """4xx other than auth/rate limit: the request itself is invalid. Never retried."""


class GenerationRefusedError(GenerationError):
    """The model refused to answer (OpenAI ``refusal``)."""


class MalformedGenerationError(GenerationError):
    """The response did not have the expected shape (or was cut off)."""


# --- request / result ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class OutputSchema:
    """Ask for JSON matching this schema (OpenAI strict structured outputs)."""

    name: str
    schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    messages: Sequence[ChatMessage]
    max_output_tokens: int = 800
    temperature: float | None = 0.0  # None: provider default (some models reject it)
    output_schema: OutputSchema | None = None


@dataclass(frozen=True, slots=True)
class GenerationUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    provider: str  # "openai", "fake"
    model: str  # the model that actually answered, as reported by the provider
    finish_reason: str
    usage: GenerationUsage
    latency_ms: float
    attempts: int = 1
    response_id: str | None = None

    def parsed_json(self) -> Any:
        """The text as JSON (for structured output). Raises MalformedGenerationError."""
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise MalformedGenerationError("the model did not return valid JSON") from exc


class GenerationProvider(Protocol):
    @property
    def model_id(self) -> str:
        """Stable identifier, e.g. 'openai:gpt-4o-mini' or 'fake:scripted'."""
        ...

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...


# --- fake -------------------------------------------------------------------------------

Responder = Callable[[GenerationRequest], str]


@dataclass
class FakeGenerationProvider:
    """Deterministic provider for tests and offline evaluation.

    ``responses`` are returned in order (a string is returned as text, an
    exception instance is raised); ``responder`` computes a reply from the
    request instead. Every request is recorded in ``requests``.
    """

    responses: list[str | GenerationError] = field(default_factory=list)
    responder: Responder | None = None
    model: str = "scripted"
    input_tokens_per_message: int = 10
    requests: list[GenerationRequest] = field(default_factory=list)

    @property
    def model_id(self) -> str:
        return f"fake:{self.model}"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        if self.responder is not None:
            text = self.responder(request)
        elif self.responses:
            item = self.responses.pop(0)
            if isinstance(item, GenerationError):
                raise item
            text = item
        else:
            raise GenerationUnavailableError("fake provider has no scripted response left")
        return GenerationResult(
            text=text,
            provider="fake",
            model=self.model,
            finish_reason="stop",
            usage=GenerationUsage(
                input_tokens=self.input_tokens_per_message * len(request.messages),
                output_tokens=len(text.split()),
            ),
            latency_ms=0.0,
        )


# --- OpenAI -----------------------------------------------------------------------------

Sleep = Callable[[float], Awaitable[None]]


class OpenAIChatProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 10.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._sleep = sleep

    @property
    def model_id(self) -> str:
        return f"openai:{self._model}"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if not request.messages:
            raise GenerationRequestError("at least one message is required")
        started = time.perf_counter()
        try:
            # One deadline for the whole call, retries included.
            async with asyncio.timeout(self._timeout):
                return await self._generate(request, started)
        except TimeoutError:
            raise GenerationTimeoutError(
                f"OpenAI chat completion exceeded {self._timeout:g}s"
            ) from None

    async def _generate(self, request: GenerationRequest, started: float) -> GenerationResult:
        payload = self._payload(request)
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        last: GenerationError = GenerationUnavailableError("no attempt made")
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                response = await self._client.post(
                    "chat/completions", json=payload, headers=headers
                )
            except httpx.TimeoutException as exc:
                last = GenerationTimeoutError(f"transport timeout: {type(exc).__name__}")
            except httpx.TransportError as exc:
                last = GenerationUnavailableError(f"transport error: {type(exc).__name__}")
            else:
                if response.status_code == 200:
                    return self._parse(response, request, started, attempts=attempt + 1)
                last = _error_for(response)
                if not last.retryable:
                    raise last
                retry_after = _retry_after_seconds(response)
            if attempt < self._max_retries:
                await self._sleep(self._delay(attempt, retry_after))
        raise type(last)(f"{last} (after {self._max_retries + 1} attempts)")

    def _payload(self, request: GenerationRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_completion_tokens": request.max_output_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_schema.name,
                    "schema": request.output_schema.schema,
                    "strict": True,
                },
            }
        return payload

    def _parse(
        self,
        response: httpx.Response,
        request: GenerationRequest,
        started: float,
        *,
        attempts: int,
    ) -> GenerationResult:
        try:
            body: dict[str, Any] = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            refusal = message.get("refusal")
            content = message.get("content")
            finish_reason = str(choice.get("finish_reason") or "unknown")
            usage = body.get("usage") or {}
            result_usage = GenerationUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            )
            model = str(body.get("model") or self._model)
            response_id = body.get("id")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MalformedGenerationError("OpenAI returned a malformed chat response") from exc
        if refusal:
            raise GenerationRefusedError("the model refused to answer")
        if not isinstance(content, str) or not content.strip():
            raise MalformedGenerationError("OpenAI returned an empty message")
        if finish_reason == "length" and request.output_schema is not None:
            # Truncated JSON is not valid output; the token limit is too small.
            raise MalformedGenerationError("structured output was cut off at the token limit")
        return GenerationResult(
            text=content,
            provider="openai",
            model=model,
            finish_reason=finish_reason,
            usage=result_usage,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            attempts=attempts,
            response_id=None if response_id is None else str(response_id),
        )

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._max_delay)
        backoff = self._base_delay * 2**attempt
        return float(min(self._max_delay, backoff * random.uniform(0.5, 1.0)))  # noqa: S311


def _error_for(response: httpx.Response) -> GenerationError:
    status = response.status_code
    detail = f"HTTP {status}: {_error_message(response)}"
    if status in {401, 403}:
        return GenerationAuthenticationError(f"OpenAI rejected the credentials ({detail})")
    if status == 429:
        return GenerationRateLimitedError(f"OpenAI rate limit ({detail})")
    if status in _RETRYABLE_STATUS:
        return GenerationUnavailableError(f"OpenAI unavailable ({detail})")
    return GenerationRequestError(f"OpenAI rejected the request ({detail})")


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


def build_generation_provider(
    settings: Settings, http_client: httpx.AsyncClient | None = None
) -> GenerationProvider | None:
    """Provider selected by settings, or None when generation is disabled.
    The caller owns (and closes) ``http_client``."""
    if settings.llm_provider == "disabled":
        return None
    if http_client is None:
        raise ValueError("the openai generation provider needs an httpx.AsyncClient")
    return OpenAIChatProvider(
        http_client,
        api_key=settings.openai_api_key,
        model=settings.openai_chat_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
