"""The LangChain layer: adapter behaviour, prompt templating and chain composition."""

import json
from typing import Any
from uuid import UUID

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.ai.orchestration.chains import chat_prompt, grounded_answer_chain
from app.ai.orchestration.chat_model import ContextLedgerChatModel, to_chat_messages
from app.ai.prompts import get_prompt
from app.ai.prompts.registry import PromptTemplate
from app.ai.providers import (
    FakeGenerationProvider,
    GenerationRateLimitedError,
    GenerationRequestError,
    OutputSchema,
)

INPUTS = {"question": "limit?", "valid_at": "T", "known_at": "K", "facts": '[F1] x = {"a": 1}'}


async def test_the_adapter_calls_the_provider_and_reports_usage() -> None:
    fake = FakeGenerationProvider(responses=["hello world"])
    model = ContextLedgerChatModel(provider=fake, max_output_tokens=123, temperature=None)

    message = await model.ainvoke([SystemMessage("rules"), HumanMessage("question")])

    assert isinstance(message, AIMessage) and message.content == "hello world"
    assert message.usage_metadata == {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22}
    assert message.response_metadata["provider"] == "fake"
    assert message.response_metadata["model"] == "scripted"
    [request] = fake.requests
    assert [(m.role, m.content) for m in request.messages] == [
        ("system", "rules"),
        ("user", "question"),
    ]
    assert request.max_output_tokens == 123 and request.temperature is None


async def test_bound_options_reach_the_provider() -> None:
    fake = FakeGenerationProvider(responses=["{}"])
    schema = OutputSchema("s", {"type": "object"})
    model = ContextLedgerChatModel(provider=fake).bind(output_schema=schema, max_output_tokens=50)

    await model.ainvoke([HumanMessage("q")])

    assert fake.requests[0].output_schema is schema
    assert fake.requests[0].max_output_tokens == 50


async def test_provider_errors_propagate_unchanged() -> None:
    model = ContextLedgerChatModel(
        provider=FakeGenerationProvider(responses=[GenerationRateLimitedError("quota")])
    )
    with pytest.raises(GenerationRateLimitedError):
        await model.ainvoke([HumanMessage("q")])


def test_the_adapter_is_async_only() -> None:
    model = ContextLedgerChatModel(provider=FakeGenerationProvider(responses=["x"]))
    with pytest.raises(NotImplementedError):
        model.invoke([HumanMessage("q")])


async def test_unsupported_inputs_are_rejected() -> None:
    with pytest.raises(GenerationRequestError):
        to_chat_messages([ToolMessage("x", tool_call_id="1")])
    with pytest.raises(GenerationRequestError, match="stop"):
        await ContextLedgerChatModel(provider=FakeGenerationProvider()).ainvoke(
            [HumanMessage("q")], stop=["\n"]
        )


def test_system_text_is_literal_and_user_placeholders_are_checked() -> None:
    template = PromptTemplate(
        "test-v1",
        "t",
        system='Return {"answer": ...}',
        user="{question}",
        placeholders=frozenset({"question"}),
    )
    rendered = chat_prompt(template).format_messages(question="Q")
    assert rendered[0].content == 'Return {"answer": ...}'
    assert rendered[1].content == "Q"
    broken = PromptTemplate(
        "test-v2", "t", system="s", user="{question} {extra}", placeholders=frozenset({"question"})
    )
    with pytest.raises(ValueError, match="placeholders"):
        chat_prompt(broken)


class Recorder(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.starts: list[list[BaseMessage]] = []
        self.metadata: list[dict[str, Any]] = []

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.starts.extend(messages)
        self.metadata.append(metadata or {})


async def test_the_grounded_answer_chain_renders_the_versioned_prompt() -> None:
    reply = json.dumps(
        {"answer": "a", "insufficient_evidence": False, "cited_facts": ["F1"], "inferences": []}
    )
    fake = FakeGenerationProvider(responses=[reply])
    template = get_prompt("grounded-answer-v2")
    recorder = Recorder()

    message = await grounded_answer_chain(template, ContextLedgerChatModel(provider=fake)).ainvoke(
        INPUTS, config={"callbacks": [recorder]}
    )

    assert message.content == reply
    [request] = fake.requests
    assert request.messages[0].content == template.system
    assert request.messages[1].content == template.render_user(**INPUTS)
    assert request.output_schema is not None and request.output_schema.name == "grounded_answer"
    # Callbacks see the run (tracing hook) with the prompt version in its metadata.
    assert recorder.metadata[0]["prompt_version"] == "grounded-answer-v2"
    assert recorder.metadata[0]["prompt_fingerprint"] == template.fingerprint
