"""Generation providers, without any network: OpenAI is simulated with httpx.MockTransport."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.ai.providers import (
    ChatMessage,
    FakeGenerationProvider,
    GenerationAuthenticationError,
    GenerationRateLimitedError,
    GenerationRefusedError,
    GenerationRequest,
    GenerationRequestError,
    GenerationTimeoutError,
    GenerationUnavailableError,
    MalformedGenerationError,
    OpenAIChatProvider,
    OutputSchema,
    build_generation_provider,
)
from app.core.config import Environment, Settings

KEY = "sk-test-not-a-real-key"
REQUEST = GenerationRequest(
    messages=[ChatMessage("system", "Answer from evidence."), ChatMessage("user", "Limit?")],
    max_output_tokens=200,
)
Handler = Callable[[httpx.Request], httpx.Response]


def completion(content: str | None = "The limit is 5000.", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-123",
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129},
    }
    body.update(overrides)
    return body


class Recorder:
    """Serves scripted responses and records every request."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def no_sleep(seconds: float) -> None:
    return None


def provider(handler: Handler, **kwargs: Any) -> OpenAIChatProvider:
    client = httpx.AsyncClient(
        base_url="https://api.openai.test/v1/", transport=httpx.MockTransport(handler)
    )
    options: dict[str, Any] = {"max_retries": 2, "sleep": no_sleep}
    options.update(kwargs)
    return OpenAIChatProvider(client, api_key=SecretStr(KEY), model="gpt-4o-mini", **options)


# --- success -------------------------------------------------------------------------------


async def test_a_successful_completion_returns_text_usage_and_model() -> None:
    recorder = Recorder(httpx.Response(200, json=completion()))

    result = await provider(recorder).generate(REQUEST)

    assert result.text == "The limit is 5000."
    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini-2024-07-18"  # as reported by the API
    assert result.finish_reason == "stop"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (120, 9)
    assert result.usage.total_tokens == 129
    assert result.attempts == 1 and result.response_id == "chatcmpl-123"
    assert result.latency_ms >= 0


async def test_the_request_sent_to_openai() -> None:
    recorder = Recorder(httpx.Response(200, json=completion()))

    await provider(recorder).generate(REQUEST)

    [sent] = recorder.requests
    body = json.loads(sent.content)
    assert sent.url.path == "/v1/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert body == {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Answer from evidence."},
            {"role": "user", "content": "Limit?"},
        ],
        "max_completion_tokens": 200,
        "temperature": 0.0,
    }


async def test_structured_output_asks_for_a_strict_json_schema() -> None:
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    recorder = Recorder(httpx.Response(200, json=completion('{"answer": "5000"}')))
    request = GenerationRequest(
        messages=REQUEST.messages, output_schema=OutputSchema("grounded_answer", schema)
    )

    result = await provider(recorder).generate(request)

    body = json.loads(recorder.requests[0].content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "grounded_answer", "schema": schema, "strict": True},
    }
    assert result.parsed_json() == {"answer": "5000"}


async def test_temperature_can_be_left_to_the_provider_default() -> None:
    recorder = Recorder(httpx.Response(200, json=completion()))
    request = GenerationRequest(messages=REQUEST.messages, temperature=None)

    await provider(recorder).generate(request)

    assert "temperature" not in json.loads(recorder.requests[0].content)


# --- retries and failures ------------------------------------------------------------------


async def test_rate_limits_and_server_errors_are_retried() -> None:
    recorder = Recorder(
        httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "1"}),
        httpx.Response(503, json={"error": {"message": "overloaded"}}),
        httpx.Response(200, json=completion()),
    )
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    result = await provider(recorder, sleep=record_sleep).generate(REQUEST)

    assert result.attempts == 3
    assert len(recorder.requests) == 3
    assert delays[0] == 1.0  # Retry-After honoured


async def test_a_persistent_rate_limit_raises_after_the_last_retry() -> None:
    recorder = Recorder(*[httpx.Response(429, json={"error": {"message": "quota"}})] * 3)

    with pytest.raises(GenerationRateLimitedError, match="after 3 attempts"):
        await provider(recorder).generate(REQUEST)
    assert len(recorder.requests) == 3


async def test_authentication_errors_are_not_retried_and_do_not_leak_the_key() -> None:
    recorder = Recorder(httpx.Response(401, json={"error": {"message": "Incorrect API key"}}))

    with pytest.raises(GenerationAuthenticationError) as caught:
        await provider(recorder).generate(REQUEST)

    assert len(recorder.requests) == 1
    assert KEY not in str(caught.value)


async def test_invalid_requests_are_not_retried() -> None:
    recorder = Recorder(httpx.Response(400, json={"error": {"message": "bad model"}}))

    with pytest.raises(GenerationRequestError, match="bad model"):
        await provider(recorder).generate(REQUEST)
    assert len(recorder.requests) == 1


async def test_transport_errors_are_retried_then_reported() -> None:
    recorder = Recorder(*[httpx.ConnectError("refused")] * 3)

    with pytest.raises(GenerationUnavailableError, match="ConnectError"):
        await provider(recorder).generate(REQUEST)


async def test_the_whole_call_has_one_deadline() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=completion())

    client = httpx.AsyncClient(base_url="https://x/v1/", transport=httpx.MockTransport(hang))
    slow = OpenAIChatProvider(
        client, api_key=SecretStr(KEY), model="m", timeout_seconds=0.05, sleep=no_sleep
    )

    with pytest.raises(GenerationTimeoutError):
        await slow.generate(REQUEST)


async def test_cancellation_is_not_swallowed() -> None:
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(5)
        return httpx.Response(200, json=completion())

    client = httpx.AsyncClient(base_url="https://x/v1/", transport=httpx.MockTransport(hang))
    task = asyncio.create_task(
        OpenAIChatProvider(client, api_key=SecretStr(KEY), model="m").generate(REQUEST)
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- malformed responses ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"no": "choices"},
        completion(content=None),
        completion(content="   "),
    ],
    ids=["no-choices", "missing-choices", "null-content", "blank-content"],
)
async def test_malformed_responses_raise(body: dict[str, Any]) -> None:
    with pytest.raises(MalformedGenerationError):
        await provider(Recorder(httpx.Response(200, json=body))).generate(REQUEST)


async def test_non_json_bodies_raise() -> None:
    with pytest.raises(MalformedGenerationError):
        await provider(Recorder(httpx.Response(200, text="<html>"))).generate(REQUEST)


async def test_refusals_raise_instead_of_becoming_answers() -> None:
    body = completion(content=None)
    body["choices"][0]["message"]["refusal"] = "I can't help with that."

    with pytest.raises(GenerationRefusedError):
        await provider(Recorder(httpx.Response(200, json=body))).generate(REQUEST)


async def test_truncated_structured_output_raises() -> None:
    body = completion(content='{"answer": "50')
    body["choices"][0]["finish_reason"] = "length"
    request = GenerationRequest(
        messages=REQUEST.messages, output_schema=OutputSchema("x", {"type": "object"})
    )

    with pytest.raises(MalformedGenerationError, match="cut off"):
        await provider(Recorder(httpx.Response(200, json=body))).generate(request)


async def test_invalid_json_is_reported_when_parsed() -> None:
    result = await provider(Recorder(httpx.Response(200, json=completion("not json")))).generate(
        REQUEST
    )
    with pytest.raises(MalformedGenerationError):
        result.parsed_json()


async def test_an_empty_conversation_is_rejected_before_any_call() -> None:
    recorder = Recorder()
    with pytest.raises(GenerationRequestError):
        await provider(recorder).generate(GenerationRequest(messages=[]))
    assert recorder.requests == []


# --- fake provider ---------------------------------------------------------------------------


async def test_the_fake_provider_replays_scripts_and_records_requests() -> None:
    fake = FakeGenerationProvider(responses=["first", GenerationRateLimitedError("scripted")])

    first = await fake.generate(REQUEST)
    with pytest.raises(GenerationRateLimitedError):
        await fake.generate(REQUEST)
    with pytest.raises(GenerationUnavailableError):
        await fake.generate(REQUEST)

    assert first.text == "first" and first.provider == "fake"
    assert first.usage.input_tokens == 20 and first.usage.output_tokens == 1
    assert fake.model_id == "fake:scripted"
    assert len(fake.requests) == 3 and fake.requests[0] is REQUEST


async def test_the_fake_provider_can_compute_replies() -> None:
    fake = FakeGenerationProvider(responder=lambda r: r.messages[-1].content.upper())
    assert (await fake.generate(REQUEST)).text == "LIMIT?"


# --- configuration ---------------------------------------------------------------------------


def test_generation_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST)
    assert settings.llm_provider == "disabled"
    assert build_generation_provider(settings) is None


def test_openai_generation_needs_a_key() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, llm_provider="openai")


async def test_openai_generation_is_built_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key=SecretStr(KEY),
        openai_chat_model="gpt-4.1-mini",
        llm_timeout_seconds=12,
        llm_max_retries=1,
    )
    async with httpx.AsyncClient() as client:
        built = build_generation_provider(settings, client)
    assert built is not None and built.model_id == "openai:gpt-4.1-mini"
    with pytest.raises(ValueError, match="httpx"):
        build_generation_provider(settings)
