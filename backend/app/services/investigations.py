"""Investigations: run the Historical Decision Investigator for a caller and keep its trace.

Authorization happens in three layers:

1. Here, before any model call: the caller needs ``facts:read`` AND
   ``decisions:read`` (membership, role and token scopes, re-read from the
   database). A caller who could not use the tools does not spend tokens.
2. In every tool call: the existing services check the same permissions again,
   plus privacy ceilings, inside their own transactions.
3. After the run: citations are checked against ids the tools returned.

Every run, including a failed one, is stored in ``agent_runs`` (append-only).
A stored run can be read back only by the user who requested it, in the same
organization: its answer was produced under that user's permissions.
"""

from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.investigator import (
    DecisionInvestigator,
    Investigation,
    InvestigationError,
    InvestigationStatus,
)
from app.ai.agent.tools import ToolRecord
from app.domain.errors import NotFoundError
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.models.agent_run import AgentRun
from app.services.authorization import require_permission

AGENT_NAME = "decision-investigator"


class InvestigationService:
    def __init__(
        self, session: AsyncSession, investigator: DecisionInvestigator | None = None
    ) -> None:
        self._session = session
        self._investigator = investigator

    async def investigate(self, ctx: TenantContext, question: str) -> Investigation:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            await require_permission(self._session, ctx, Permission.READ_DECISIONS)
        if self._investigator is None:  # pragma: no cover - wiring error
            raise RuntimeError("InvestigationService was built without an investigator")
        try:
            result = await self._investigator.investigate(ctx, question)
        except InvestigationError as exc:
            if exc.partial is not None:
                await self._save(ctx, exc.partial)
            raise
        await self._save(ctx, result)
        return result

    async def get(self, ctx: TenantContext, run_id: UUID) -> Investigation:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_DECISIONS)
            row = await self._session.scalar(
                select(AgentRun).where(
                    AgentRun.organization_id == ctx.organization_id,
                    AgentRun.id == run_id,
                    AgentRun.requested_by_user_id == ctx.user_id,
                )
            )
            if row is None:
                raise NotFoundError("investigation not found")
            return _from_row(row)

    async def _save(self, ctx: TenantContext, result: Investigation) -> None:
        async with self._session.begin():
            self._session.add(
                AgentRun(
                    id=result.run_id,
                    organization_id=ctx.organization_id,
                    requested_by_user_id=ctx.user_id,
                    agent_client_id=ctx.agent_client_id,
                    agent=AGENT_NAME,
                    question=result.question,
                    status=result.status.value,
                    answer=result.answer,
                    cited_ids=list(result.cited_ids),
                    rejected_ids=list(result.rejected_ids),
                    tool_calls=[asdict(record) for record in result.tool_calls],
                    prompt_version=result.prompt_version,
                    model=result.model,
                    steps=result.steps,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                    error=result.error,
                )
            )


def _from_row(row: AgentRun) -> Investigation:
    return Investigation(
        run_id=row.id,
        question=row.question,
        status=InvestigationStatus(row.status),
        answer=row.answer,
        cited_ids=list(row.cited_ids),
        rejected_ids=list(row.rejected_ids),
        prompt_version=row.prompt_version,
        model=row.model,
        steps=row.steps,
        tool_calls=[ToolRecord(**record) for record in row.tool_calls],
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        latency_ms=row.latency_ms,
        error=row.error,
    )
