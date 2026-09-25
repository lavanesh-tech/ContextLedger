"""The Historical Decision Investigator: "why was decision X made, and would it change?"

A deliberately small agent loop (LangChain tools + our chat model; no LangGraph):

1. The model gets the question and four read-only tools (``tools.py``).
2. Each step, the model either calls tools or returns the final JSON answer.
3. Limits: ``max_steps`` model calls and ``max_tool_calls`` tool calls in total.
   Running out returns ``step_limit`` instead of a guessed answer.
4. The final answer's ``cited_ids`` are checked against ids the tools actually
   returned; any invented id makes the result ``ungrounded`` and the text is withheld.

Tool failures go back to the model as tool results; provider failures raise
``InvestigationError``. Everything the agent did is returned as a trace.
"""

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from app.ai.agent.tools import InvestigatorBackend, ToolLedger, ToolRecord, build_tools
from app.ai.orchestration.chat_model import ContextLedgerChatModel
from app.ai.prompts import get_prompt
from app.ai.prompts.investigator import DEFAULT_INVESTIGATOR_PROMPT
from app.ai.providers import GenerationError, GenerationProvider, OutputSchema
from app.domain.errors import ValidationFailedError
from app.domain.tenancy import TenantContext

MAX_QUESTION_CHARS = 1000


class InvestigationStatus(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNGROUNDED = "ungrounded"
    STEP_LIMIT = "step_limit"
    FAILED = "failed"  # the model provider failed; recorded in the trace, never answered


class InvestigationError(Exception):
    def __init__(self, message: str, *, cause: str, partial: "Investigation | None" = None) -> None:
        super().__init__(message)
        self.cause = cause
        self.partial = partial  # what ran before the failure, for the trace


@dataclass
class Investigation:
    run_id: UUID
    question: str
    status: InvestigationStatus
    answer: str | None
    cited_ids: list[str]
    rejected_ids: list[str]
    prompt_version: str
    model: str
    steps: int
    tool_calls: list[ToolRecord] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: str | None = None


class DecisionInvestigator:
    def __init__(
        self,
        backend: InvestigatorBackend,
        generator: GenerationProvider,
        *,
        prompt_version: str = DEFAULT_INVESTIGATOR_PROMPT,
        max_steps: int = 6,
        max_tool_calls: int = 12,
        max_output_tokens: int = 800,
    ) -> None:
        self._backend = backend
        self._generator = generator
        self._prompt = get_prompt(prompt_version)
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_output_tokens = max_output_tokens

    async def investigate(self, ctx: TenantContext, question: str) -> Investigation:
        question = question.strip()
        if not question or len(question) > MAX_QUESTION_CHARS:
            raise ValidationFailedError(f"question must be 1-{MAX_QUESTION_CHARS} characters")
        started = time.perf_counter()
        ledger = ToolLedger()
        tools = {t.name: t for t in build_tools(ctx, self._backend, ledger)}
        schema = self._prompt.output_schema
        assert schema is not None  # noqa: S101 - the investigator prompt always has one
        model = ContextLedgerChatModel(provider=self._generator).bind_tools(
            list(tools.values()),
            output_schema=OutputSchema(name="investigation", schema=schema),
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
        )
        messages: list[BaseMessage] = [
            SystemMessage(self._prompt.system),
            HumanMessage(self._prompt.render_user(question=question)),
        ]
        result = Investigation(
            run_id=uuid4(),
            question=question,
            status=InvestigationStatus.STEP_LIMIT,
            answer=None,
            cited_ids=[],
            rejected_ids=[],
            prompt_version=self._prompt.version,
            model=self._generator.model_id,
            steps=0,
            tool_calls=ledger.records,
        )
        config: Any = {
            "run_name": "decision_investigator",
            "tags": ["contextledger", "investigator"],
            "metadata": {"prompt_version": self._prompt.version, "run_id": str(result.run_id)},
        }
        calls_made = 0
        while result.steps < self._max_steps:
            result.steps += 1
            try:
                reply = await model.ainvoke(messages, config=config)
            except GenerationError as exc:
                self._fail(result, started, type(exc).__name__)
                raise InvestigationError(
                    "the model could not complete the investigation",
                    cause=type(exc).__name__,
                    partial=result,
                ) from exc
            if reply.usage_metadata is not None:
                result.input_tokens += reply.usage_metadata["input_tokens"]
                result.output_tokens += reply.usage_metadata["output_tokens"]
            messages.append(reply)
            if not reply.tool_calls:
                try:
                    self._finish(result, reply, ledger)
                except InvestigationError as exc:
                    self._fail(result, started, exc.cause)
                    exc.partial = result
                    raise
                break
            for call in reply.tool_calls:
                calls_made += 1
                content = (
                    json.dumps({"error": "tool call budget exhausted"})
                    if calls_made > self._max_tool_calls
                    else await _run_tool(tools, call["name"], call["args"])
                )
                messages.append(ToolMessage(content=content, tool_call_id=str(call["id"])))
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    @staticmethod
    def _fail(result: Investigation, started: float, cause: str) -> None:
        result.status = InvestigationStatus.FAILED
        result.error = cause
        result.answer = None
        result.latency_ms = int((time.perf_counter() - started) * 1000)

    @staticmethod
    def _finish(result: Investigation, reply: AIMessage, ledger: ToolLedger) -> None:
        try:
            data = json.loads(str(reply.content))
            answer = str(data["answer"])
            insufficient = bool(data["insufficient_evidence"])
            cited = [str(i) for i in data["cited_ids"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise InvestigationError(
                "the model returned a malformed investigation", cause="MalformedGenerationError"
            ) from exc
        result.rejected_ids = [i for i in cited if i not in ledger.observed]
        result.cited_ids = [i for i in cited if i in ledger.observed]
        if result.rejected_ids or (not insufficient and not result.cited_ids):
            result.status = InvestigationStatus.UNGROUNDED
            result.answer = None
        elif insufficient:
            result.status = InvestigationStatus.INSUFFICIENT_EVIDENCE
            result.answer = answer
        else:
            result.status = InvestigationStatus.ANSWERED
            result.answer = answer


async def _run_tool(tools: dict[str, BaseTool], name: str, args: dict[str, Any]) -> str:
    tool = tools.get(name)
    if tool is None:
        return json.dumps({"error": f"unknown tool {name!r}"})
    try:
        output = await tool.ainvoke(args)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
        return json.dumps({"error": f"invalid arguments: {', '.join(fields)}"})
    return str(output)
