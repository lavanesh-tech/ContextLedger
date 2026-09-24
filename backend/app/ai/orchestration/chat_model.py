"""A LangChain chat model backed by ContextLedger's ``GenerationProvider``.

Why an adapter instead of ``langchain-openai``: model access already lives in one
tested place (app/ai/providers.py) with one deadline per call, bounded retries,
typed errors and a fake for tests. Wrapping it keeps that single boundary, so a
chain or an agent can never bypass the cost and failure controls, and CI never
needs a network or an API key.

Bound call options (``model.bind(...)``): ``output_schema`` (OutputSchema),
``max_output_tokens`` and ``temperature``. Errors from the provider propagate
unchanged (``GenerationError`` subclasses).
"""

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from app.ai.providers import (
    ChatMessage,
    GenerationProvider,
    GenerationRequest,
    GenerationRequestError,
    OutputSchema,
    Role,
)


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
        else:
            raise GenerationRequestError(f"unsupported message type {type(message).__name__}")
        if not isinstance(message.content, str):
            raise GenerationRequestError("only text message content is supported")
        converted.append(ChatMessage(role, message.content))
    return converted


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
        )
        result = await self._provider.generate(request)
        message = AIMessage(
            content=result.text,
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
