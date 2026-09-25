"""Agent runs: the immutable trace of each Historical Decision Investigator run.

Append-only (trigger ``trg_agent_runs_immutable``, migration 0011). A row records
who asked, what the agent did (every tool call with validated arguments, outcome
and latency), which prompt and model it used, tokens, and the result. Tool
outputs are not stored: they can contain fact values, which stay governed by the
services' privacy checks when they are read again.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantOwnedMixin, UUIDPrimaryKeyMixin


class AgentRun(UUIDPrimaryKeyMixin, TenantOwnedMixin, Base):
    __tablename__ = "agent_runs"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 (read by SQLAlchemy)
    __table_args__ = (
        Index("ix_agent_runs_organization_id_created_at", "organization_id", "created_at"),
        CheckConstraint(
            "status IN ('answered', 'insufficient_evidence', 'ungrounded', 'step_limit', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "steps >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="counters_non_negative",
        ),
    )

    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    agent_client_id: Mapped[uuid.UUID | None]
    agent: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32))
    answer: Mapped[str | None] = mapped_column(Text)
    cited_ids: Mapped[list[str]] = mapped_column(JSONB)
    rejected_ids: Mapped[list[str]] = mapped_column(JSONB)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    prompt_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100))
    steps: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
