"""Event envelope, per-type payload schemas, and topic routing.

Every event is JSON with a CloudEvents-style envelope::

    {"specversion": "1.0", "id": "<event uuid>", "type": "fact.version_recorded",
     "source": "contextledger", "schema_version": 1,
     "organization_id": "<tenant uuid>", "subject": "<aggregate uuid>",
     "time": "2026-01-15T09:00:00+00:00", "data": {...}}

* ``id`` is unique per event and is what consumers deduplicate on.
* The Kafka message **key is the organization id**, so all events of one tenant
  land in one partition, in outbox order (per-tenant ordering, parallelism
  across tenants).
* ``data`` holds identifiers and metadata only. Fact values, evidence excerpts
  and decision outcomes never leave PostgreSQL through Kafka, because consumers
  do not enforce privacy scopes; a consumer that needs content reads it through
  the services, with authorization.
* Schemas evolve additively. Payload models ignore unknown fields, so adding a
  field is compatible; removing or retyping one needs a new ``schema_version``
  (and, if incompatible, a new ``.v2`` topic).
"""

from typing import Any, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.domain.facts import PrivacyScope

FACTS_TOPIC: Final = "contextledger.facts.v1"
EVIDENCE_TOPIC: Final = "contextledger.evidence.v1"
DECISIONS_TOPIC: Final = "contextledger.decisions.v1"
TOPICS: Final = (FACTS_TOPIC, EVIDENCE_TOPIC, DECISIONS_TOPIC)
DLQ_SUFFIX: Final = ".dlq"


def dead_letter_topic(topic: str) -> str:
    return topic + DLQ_SUFFIX


class EventData(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class FactVersionRecorded(EventData):
    id: UUID
    fact_id: UUID
    version: int
    source_id: UUID
    supersedes_id: UUID | None
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None
    authority: int
    privacy_scope: PrivacyScope
    recorded_at: AwareDatetime


class FactEmbeddingStored(EventData):
    fact_version_id: UUID
    model: str
    created_at: AwareDatetime


class EvidenceCaptured(EventData):
    id: UUID
    source_id: UUID
    evidence_type: str
    content_sha256: str
    privacy_scope: PrivacyScope
    captured_at: AwareDatetime
    recorded_at: AwareDatetime


class EvidenceLinked(EventData):
    fact_version_id: UUID
    evidence_id: UUID
    relation: str
    linked_at: AwareDatetime


class ContextCaptured(EventData):
    id: UUID
    captured_by_user_id: UUID
    valid_at: AwareDatetime
    known_at: AwareDatetime
    embedding_model: str
    created_at: AwareDatetime


class DecisionRecorded(EventData):
    id: UUID
    snapshot_id: UUID
    decided_by_user_id: UUID
    agent: str | None
    action: str
    decided_at: AwareDatetime
    receipt_sha256: str


class ContradictionDetected(EventData):
    id: UUID
    entity_id: UUID
    left_version_id: UUID
    right_version_id: UUID
    kind: str
    detector: str
    privacy_scope: str
    detected_at: AwareDatetime


# event type -> (topic, payload schema). The database triggers (migration 0010)
# emit exactly these types (contradiction.detected: migration 0012).
EVENT_TYPES: Final[dict[str, tuple[str, type[EventData]]]] = {
    "fact.version_recorded": (FACTS_TOPIC, FactVersionRecorded),
    "fact.embedding_stored": (FACTS_TOPIC, FactEmbeddingStored),
    "contradiction.detected": (FACTS_TOPIC, ContradictionDetected),
    "evidence.captured": (EVIDENCE_TOPIC, EvidenceCaptured),
    "evidence.linked": (EVIDENCE_TOPIC, EvidenceLinked),
    "context.captured": (DECISIONS_TOPIC, ContextCaptured),
    "decision.recorded": (DECISIONS_TOPIC, DecisionRecorded),
}


class UnknownEventTypeError(ValueError):
    pass


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specversion: Literal["1.0"] = "1.0"
    id: UUID
    type: str
    source: Literal["contextledger"] = "contextledger"
    schema_version: int = 1
    organization_id: UUID
    subject: UUID
    time: AwareDatetime
    data: dict[str, Any]

    def payload(self) -> EventData:
        """The typed payload (raises for unknown types or invalid data)."""
        if self.type not in EVENT_TYPES:
            raise UnknownEventTypeError(self.type)
        return EVENT_TYPES[self.type][1].model_validate(self.data)


def topic_for(event_type: str) -> str:
    if event_type not in EVENT_TYPES:
        raise UnknownEventTypeError(event_type)
    return EVENT_TYPES[event_type][0]


def json_schemas() -> dict[str, Any]:
    """JSON Schema of the envelope and every payload (committed as docs/events/schemas.json)."""
    return {
        "envelope": EventEnvelope.model_json_schema(),
        "events": {
            event_type: {"topic": topic, "data": model.model_json_schema()}
            for event_type, (topic, model) in sorted(EVENT_TYPES.items())
        },
    }
