"""Transactional outbox feeding the Neo4j provenance graph.

Rows are written by database triggers (migration 0008) in the same transaction
as the change they describe, so the graph can never silently miss a write. The
projector (app/workers/graph.py) claims rows with ``FOR UPDATE SKIP LOCKED``,
re-reads the current state of each referenced row, writes it to Neo4j with
idempotent ``MERGE`` statements, and deletes the outbox rows it handled.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantOwnedMixin


class GraphOutboxEvent(TenantOwnedMixin, Base):
    __tablename__ = "graph_outbox"
    __table_args__ = (Index("ix_graph_outbox_organization_id_id", "organization_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    table_name: Mapped[str] = mapped_column(String(64))
    # Primary-key columns of the changed row, e.g. {"id": "..."} or
    # {"fact_version_id": "...", "evidence_id": "..."}.
    row_key: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
