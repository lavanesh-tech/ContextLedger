"""SQLAlchemy ORM models (PostgreSQL is the system of record).

Every model module must be imported here so that ``Base.metadata`` is complete
when Alembic autogenerates or checks migrations.
"""

from app.db.base import Base
from app.models.agent_client import AgentClient
from app.models.agent_run import AgentRun
from app.models.decision import (
    ContextSnapshot,
    ContextSnapshotFact,
    Decision,
    DecisionFact,
)
from app.models.embedding import EmbeddingJob, FactEmbedding
from app.models.entity import Entity
from app.models.events import OrganizationActivityDaily, OutboxEvent, ProcessedEvent
from app.models.evidence import Evidence, FactVersionEvidence
from app.models.fact import Fact, FactVersion
from app.models.membership import OrganizationMembership
from app.models.organization import Organization
from app.models.outbox import GraphOutboxEvent
from app.models.search import FactSearchDocument
from app.models.source import FactSource
from app.models.user import User

__all__ = [
    "AgentClient",
    "AgentRun",
    "Base",
    "ContextSnapshot",
    "ContextSnapshotFact",
    "Decision",
    "DecisionFact",
    "EmbeddingJob",
    "Entity",
    "Evidence",
    "Fact",
    "FactEmbedding",
    "FactSearchDocument",
    "FactSource",
    "FactVersion",
    "FactVersionEvidence",
    "GraphOutboxEvent",
    "Organization",
    "OrganizationActivityDaily",
    "OrganizationMembership",
    "OutboxEvent",
    "ProcessedEvent",
    "User",
]
