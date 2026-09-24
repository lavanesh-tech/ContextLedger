"""Domain events (Kafka) and the tables around them.

* ``event_outbox``: written by database triggers (migration 0010) in the same
  transaction as the change, published to Kafka by the relay
  (app/workers/event_relay.py) and then deleted. A committed change always
  produces its event; a rolled-back change never does.
* ``processed_events``: one row per (consumer, event id), inserted in the same
  transaction as the consumer's effect. A redelivered event finds its row and is
  skipped, which turns at-least-once delivery into an effect applied once.
* ``organization_activity_daily``: a read model maintained by a consumer.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantOwnedMixin


class OutboxEvent(TenantOwnedMixin, Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        UniqueConstraint("event_id"),
        Index("ix_event_outbox_organization_id_id", "organization_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(server_default=text("gen_random_uuid()"))
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[uuid.UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class ProcessedEvent(TenantOwnedMixin, Base):
    __tablename__ = "processed_events"
    __table_args__ = (
        Index(
            "ix_processed_events_organization_id_processed_at", "organization_id", "processed_at"
        ),
    )

    consumer: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class OrganizationActivityDaily(TenantOwnedMixin, Base):
    __tablename__ = "organization_activity_daily"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    facts_recorded: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    evidence_captured: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    decisions_recorded: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
