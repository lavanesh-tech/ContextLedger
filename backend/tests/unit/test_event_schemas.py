import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.api.docs_export import EVENT_SCHEMAS_FILE, render
from app.events.schemas import (
    DECISIONS_TOPIC,
    EVENT_TYPES,
    EVIDENCE_TOPIC,
    FACTS_TOPIC,
    EventEnvelope,
    FactVersionRecorded,
    UnknownEventTypeError,
    dead_letter_topic,
    json_schemas,
    topic_for,
)

ID = "00000000-0000-0000-0000-000000000001"
AT = "2026-01-15T09:00:00+00:00"

# Payloads exactly as the database triggers build them (to_jsonb of the row).
SAMPLES: dict[str, dict[str, Any]] = {
    "fact.version_recorded": {
        "id": ID,
        "fact_id": ID,
        "version": 2,
        "source_id": ID,
        "supersedes_id": None,
        "valid_from": AT,
        "valid_until": None,
        "authority": 90,
        "privacy_scope": "INTERNAL",
        "recorded_at": "2026-01-15T09:00:00.123456+00:00",
    },
    "fact.embedding_stored": {"fact_version_id": ID, "model": "m", "created_at": AT},
    "contradiction.detected": {
        "id": ID,
        "entity_id": ID,
        "left_version_id": ID,
        "right_version_id": ID,
        "kind": "value_conflict",
        "detector": "observed-value-conflict-v1",
        "privacy_scope": "INTERNAL",
        "detected_at": AT,
    },
    "evidence.captured": {
        "id": ID,
        "source_id": ID,
        "evidence_type": "DOCUMENT_EXCERPT",
        "content_sha256": "a" * 64,
        "privacy_scope": "INTERNAL",
        "captured_at": AT,
        "recorded_at": AT,
    },
    "evidence.linked": {
        "fact_version_id": ID,
        "evidence_id": ID,
        "relation": "SUPPORTS",
        "linked_at": AT,
    },
    "context.captured": {
        "id": ID,
        "captured_by_user_id": ID,
        "valid_at": AT,
        "known_at": AT,
        "embedding_model": "m",
        "created_at": AT,
    },
    "decision.recorded": {
        "id": ID,
        "snapshot_id": ID,
        "decided_by_user_id": ID,
        "agent": None,
        "action": "credit.approve_increase",
        "decided_at": AT,
        "receipt_sha256": "b" * 64,
    },
}


def envelope(event_type: str, data: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        id=UUID(ID),
        type=event_type,
        organization_id=UUID(ID),
        subject=UUID(ID),
        time=datetime(2026, 1, 15, 9, tzinfo=UTC),
        data=data,
    )


def test_every_event_type_has_a_sample_that_validates() -> None:
    assert set(SAMPLES) == set(EVENT_TYPES)
    for event_type, data in SAMPLES.items():
        assert envelope(event_type, data).payload().model_dump(mode="json")


def test_envelopes_round_trip_through_json() -> None:
    original = envelope("fact.version_recorded", SAMPLES["fact.version_recorded"])
    decoded = EventEnvelope.model_validate_json(original.model_dump_json())
    assert decoded == original
    assert isinstance(decoded.payload(), FactVersionRecorded)


def test_payloads_ignore_unknown_fields_for_forward_compatibility() -> None:
    data = {**SAMPLES["evidence.linked"], "added_in_a_later_version": 1}
    assert envelope("evidence.linked", data).payload()


def test_invalid_payloads_and_unknown_types_are_rejected() -> None:
    with pytest.raises(ValidationError):
        envelope("fact.version_recorded", {"id": "not-a-uuid"}).payload()
    with pytest.raises(UnknownEventTypeError):
        envelope("fact.deleted", {}).payload()
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {**json.loads(envelope("evidence.linked", {}).model_dump_json()), "extra": 1}
        )


def test_topics_group_events_by_aggregate() -> None:
    assert topic_for("fact.version_recorded") == topic_for("fact.embedding_stored") == FACTS_TOPIC
    assert topic_for("evidence.linked") == EVIDENCE_TOPIC
    assert topic_for("decision.recorded") == topic_for("context.captured") == DECISIONS_TOPIC
    assert dead_letter_topic(FACTS_TOPIC) == "contextledger.facts.v1.dlq"
    with pytest.raises(UnknownEventTypeError):
        topic_for("nope")


def test_no_payload_carries_content() -> None:
    fields = {f for _, model in EVENT_TYPES.values() for f in model.model_fields}
    assert not fields & {"value", "excerpt", "outcome", "rationale", "query", "uri", "metadata"}


def test_committed_event_schemas_are_up_to_date() -> None:
    stale = "stale event schemas: run `make api-docs` and commit docs/events/"
    assert EVENT_SCHEMAS_FILE.read_text(encoding="utf-8") == render(json_schemas()), stale
