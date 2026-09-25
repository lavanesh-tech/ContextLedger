"""A LangChain chat model backed by ContextLedger's ``GenerationProvider``.

Why an adapter instead of ``langchain-openai``: model access already lives in one
tested place (app/ai/providers.py) with one deadline per call, bounded retries,
typed errors and a fake for tests. Wrapping it keeps that single boundary, so a
chain or an agent can never bypass the cost and failure controls, and CI never
needs a network or an API key.

Bound call options (``model.bind(...)``): ``output_schema`` (OutputSchema),
``max_output_tokens``, ``temperature`` and ``tools`` (ToolSpec list, set by
``bind_tools``). Tool calls come back as ``AIMessage.tool_calls``.
Errors from the provider propagate unchanged (``GenerationError`` subclasses).
"""

import time
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

from app.ai.providers import (
    ChatMessage,
    GenerationProvider,
    GenerationRequest,
    GenerationRequestError,
    OutputSchema,
    Role,
    ToolCall,
    ToolSpec,
)
from app.observability.metrics import LLM_CALLS, LLM_LATENCY, LLM_TOKENS
from app.observability.tracing import tracer


def to_chat_messages(messages: Sequence[BaseMessage]) -> list[ChatMessage]:
    converted: list[ChatMessage] = []
    for message in messages:
        role: Role
        if isinstance(message, SystemMessage):
            role = "system"
        elif isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        elif isinstance(message, ToolMessage):
            role = "tool"
        else:
            raise GenerationRequestError(f"unsupported message type {type(message).__name__}")
        if not isinstance(message.content, str):
            raise GenerationRequestError("only text message content is supported")
        if isinstance(message, AIMessage) and message.tool_calls:
            calls = tuple(
                ToolCall(id=str(c["id"]), name=c["name"], arguments=dict(c["args"]))
                for c in message.tool_calls
            )
            converted.append(ChatMessage(role, message.content, tool_calls=calls))
        elif isinstance(message, ToolMessage):
            converted.append(ChatMessage(role, message.content, tool_call_id=message.tool_call_id))
        else:
            converted.append(ChatMessage(role, message.content))
    return converted


def tool_spec(tool: BaseTool) -> ToolSpec:
    function = convert_to_openai_tool(tool)["function"]
    return ToolSpec(
        name=function["name"],
        description=function.get("description", ""),
        parameters=function.get("parameters", {"type": "object", "properties": {}}),
    )


class ContextLedgerChatModel(BaseChatModel):
    """``BaseChatModel`` over a ``GenerationProvider`` (async only)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: Any  # GenerationProvider (a Protocol, so typed as Any for pydantic)
    max_output_tokens: int = 800
    temperature: float | None = 0.0

    @property
    def _llm_type(self) -> str:
        return "contextledger-generation-provider"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_id": self._provider.model_id}

    @property
    def _provider(self) -> GenerationProvider:
        provider: GenerationProvider = self.provider
        return provider

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if tool_choice is not None:
            raise GenerationRequestError("tool_choice is not supported")
        specs: list[ToolSpec] = []
        for tool in tools:
            if not isinstance(tool, BaseTool):
                raise GenerationRequestError("only LangChain BaseTool instances can be bound")
            specs.append(tool_spec(tool))
        return self.bind(tools=specs, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("ContextLedgerChatModel is async-only; use ainvoke()")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if stop:
            raise GenerationRequestError("stop sequences are not supported")
        schema = kwargs.get("output_schema")
        if schema is not None and not isinstance(schema, OutputSchema):
            raise GenerationRequestError("output_schema must be an OutputSchema")
        request = GenerationRequest(
            messages=to_chat_messages(messages),
            max_output_tokens=int(kwargs.get("max_output_tokens", self.max_output_tokens)),
            temperature=kwargs.get("temperature", self.temperature),
            output_schema=schema,
            tools=tuple(kwargs.get("tools") or ()),
        )
        provider = self._provider
        labels = (type(provider).__name__, provider.model_id)
        started = time.perf_counter()
        with tracer.start_as_current_span("llm.generate") as span:
            span.set_attribute("gen_ai.request.model", provider.model_id)
            span.set_attribute("gen_ai.request.max_tokens", request.max_output_tokens)
            span.set_attribute("contextledger.tools", len(request.tools))
            try:
                result = await provider.generate(request)
            except Exception as exc:
                LLM_CALLS.labels(*labels, type(exc).__name__).inc()
                LLM_LATENCY.labels(*labels).observe(time.perf_counter() - started)
                raise
            span.set_attribute("gen_ai.usage.input_tokens", result.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", result.usage.output_tokens)
            span.set_attribute("contextledger.attempts", result.attempts)
        LLM_CALLS.labels(*labels, "ok").inc()
        LLM_LATENCY.labels(*labels).observe(time.perf_counter() - started)
        LLM_TOKENS.labels(*labels, "input").inc(result.usage.input_tokens)
        LLM_TOKENS.labels(*labels, "output").inc(result.usage.output_tokens)
        message = AIMessage(
            content=result.text,
            tool_calls=[
                {"id": c.id, "name": c.name, "args": c.arguments, "type": "tool_call"}
                for c in result.tool_calls
            ],
            usage_metadata={
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
            response_metadata={
                "provider": result.provider,
                "model": result.model,
                "finish_reason": result.finish_reason,
                "attempts": result.attempts,
                "latency_ms": result.latency_ms,
                "response_id": result.response_id,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
