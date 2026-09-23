"""Pure rules for decisions and their receipts.

A **decision receipt** answers, after the fact: *what did the agent decide, when,
and exactly which facts (as they were known at that moment) did it rely on?*

The receipt hash is a SHA-256 over a canonical JSON document (sorted keys, no
whitespace, UTF-8, timestamps normalised to UTC ISO-8601). It covers the
decision, the frozen retrieval context, and every fact in that context by
version id and the hash of its value. Reading a receipt recomputes the hash from
the stored rows, so silent changes are detected even by someone who bypasses the
append-only triggers.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from app.domain.errors import ValidationFailedError
from app.domain.validation import normalize_identifier, normalize_name, validate_fact_value

RECEIPT_SCHEMA: Final = "contextledger.receipt.v1"
MAX_RATIONALE_CHARS: Final = 4000
MAX_RELIED_ON: Final = 50


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def value_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_iso(instant: datetime) -> str:
    if instant.tzinfo is None:
        raise ValidationFailedError("timestamps in receipts must be timezone-aware")
    return instant.astimezone(UTC).isoformat()


def normalize_action(action: str) -> str:
    """Actions are identifiers such as ``credit.approve_increase``."""
    return normalize_identifier(action, field="action")


def normalize_rationale(rationale: str | None) -> str | None:
    if rationale is None:
        return None
    cleaned = rationale.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_RATIONALE_CHARS:
        raise ValidationFailedError(f"rationale must be at most {MAX_RATIONALE_CHARS} characters")
    return cleaned


def normalize_agent(agent: str | None) -> str | None:
    return None if agent is None else normalize_name(agent, field="agent")


def validate_outcome(outcome: object) -> object:
    """The decision's result, as JSON (e.g. ``{"approved": true, "new_limit": 5000}``)."""
    return validate_fact_value(outcome)


def validate_relied_on(
    relied_on: Iterable[UUID], snapshot_facts: Iterable[UUID]
) -> tuple[UUID, ...]:
    """Relied-on facts must be distinct and must all come from the decision's snapshot."""
    ids = tuple(relied_on)
    if len(ids) != len(set(ids)):
        raise ValidationFailedError("relied_on contains duplicate fact versions")
    if len(ids) > MAX_RELIED_ON:
        raise ValidationFailedError(f"relied_on accepts at most {MAX_RELIED_ON} fact versions")
    missing = set(ids) - set(snapshot_facts)
    if missing:
        raise ValidationFailedError(
            "relied_on may only cite fact versions from the decision's context snapshot"
        )
    return tuple(sorted(ids))


@dataclass(frozen=True, slots=True)
class ReceiptContent:
    """Everything the receipt hash covers."""

    organization_id: UUID
    decision_id: UUID
    snapshot_id: UUID
    decided_at: datetime
    decided_by_user_id: UUID
    agent: str | None
    action: str
    outcome: object
    rationale: str | None
    query: str
    valid_at: datetime
    known_at: datetime
    embedding_model: str
    vector_search: str
    privacy_scopes: tuple[str, ...]
    # fact_version_id -> (position in the snapshot, sha256 of the value as recorded)
    facts: Mapping[UUID, tuple[int, str]]
    relied_on: frozenset[UUID]


def receipt_document(content: ReceiptContent) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "organization_id": str(content.organization_id),
        "decision": {
            "id": str(content.decision_id),
            "decided_at": utc_iso(content.decided_at),
            "decided_by_user_id": str(content.decided_by_user_id),
            "agent": content.agent,
            "action": content.action,
            "outcome": content.outcome,
            "rationale": content.rationale,
        },
        "context": {
            "snapshot_id": str(content.snapshot_id),
            "query": content.query,
            "valid_at": utc_iso(content.valid_at),
            "known_at": utc_iso(content.known_at),
            "embedding_model": content.embedding_model,
            "vector_search": content.vector_search,
            "privacy_scopes": sorted(content.privacy_scopes),
        },
        "facts": [
            {
                "fact_version_id": str(version_id),
                "position": position,
                "value_sha256": digest,
                "relied_on": version_id in content.relied_on,
            }
            for version_id, (position, digest) in sorted(
                content.facts.items(), key=lambda item: item[1][0]
            )
        ],
    }


def receipt_hash(content: ReceiptContent) -> str:
    return hashlib.sha256(canonical_json(receipt_document(content)).encode("utf-8")).hexdigest()
