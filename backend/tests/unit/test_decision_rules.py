from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.domain.decisions import (
    MAX_RATIONALE_CHARS,
    MAX_RELIED_ON,
    RECEIPT_SCHEMA,
    ReceiptContent,
    canonical_json,
    normalize_action,
    normalize_agent,
    normalize_rationale,
    receipt_document,
    receipt_hash,
    utc_iso,
    validate_outcome,
    validate_relied_on,
    value_sha256,
)
from app.domain.errors import ValidationFailedError

A, B, C = UUID(int=1), UUID(int=2), UUID(int=3)
T = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)

CONTENT = ReceiptContent(
    organization_id=UUID(int=100),
    decision_id=UUID(int=200),
    snapshot_id=UUID(int=300),
    decided_at=T,
    decided_by_user_id=UUID(int=400),
    agent="credit-review-agent v3",
    action="credit.approve_increase",
    outcome={"approved": True, "new_limit": 7500},
    rationale="Limit history and payment record support the increase.",
    query="credit limit customer-991",
    valid_at=T,
    known_at=T,
    embedding_model="deterministic:hash-v1",
    vector_search="used",
    privacy_scopes=("PUBLIC", "INTERNAL"),
    facts={A: (1, value_sha256(5000)), B: (2, value_sha256("Berlin"))},
    relied_on=frozenset({A}),
)


def test_canonical_json_is_order_independent_and_compact() -> None:
    assert canonical_json({"b": 1, "a": [1, "ü"]}) == '{"a":[1,"ü"],"b":1}'
    assert value_sha256({"x": 1, "y": 2}) == value_sha256({"y": 2, "x": 1})


def test_timestamps_are_normalised_to_utc() -> None:
    eastern = T.astimezone(timezone(timedelta(hours=-4)))

    assert utc_iso(eastern) == utc_iso(T) == "2026-01-15T10:30:00+00:00"
    with pytest.raises(ValidationFailedError, match="timezone"):
        utc_iso(datetime(2026, 1, 15))  # noqa: DTZ001 (deliberately naive)


def test_receipt_hash_is_deterministic() -> None:
    same = replace(CONTENT, facts=dict(reversed(list(CONTENT.facts.items()))))

    assert receipt_hash(CONTENT) == receipt_hash(same)
    assert len(receipt_hash(CONTENT)) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": {"approved": False, "new_limit": 7500}},
        {"rationale": "different reason"},
        {"action": "credit.deny_increase"},
        {"known_at": T + timedelta(microseconds=1)},
        {"relied_on": frozenset({A, B})},
        {"facts": {A: (1, value_sha256(5001)), B: (2, value_sha256("Berlin"))}},
        {"facts": {A: (2, value_sha256(5000)), B: (1, value_sha256("Berlin"))}},
        {"agent": None},
        {"privacy_scopes": ("PUBLIC",)},
    ],
)
def test_any_change_changes_the_hash(change: dict[str, object]) -> None:
    assert receipt_hash(replace(CONTENT, **change)) != receipt_hash(CONTENT)  # type: ignore[arg-type]


def test_receipt_document_lists_facts_by_position() -> None:
    document = receipt_document(CONTENT)

    assert document["schema"] == RECEIPT_SCHEMA
    assert [f["position"] for f in document["facts"]] == [1, 2]
    assert [f["relied_on"] for f in document["facts"]] == [True, False]


# --- inputs ------------------------------------------------------------------------


def test_relied_on_must_come_from_the_snapshot() -> None:
    assert validate_relied_on([B, A], [A, B, C]) == (A, B)
    assert validate_relied_on([], [A]) == ()
    with pytest.raises(ValidationFailedError, match="context snapshot"):
        validate_relied_on([C], [A, B])
    with pytest.raises(ValidationFailedError, match="duplicate"):
        validate_relied_on([A, A], [A])


def test_relied_on_is_bounded() -> None:
    ids = [UUID(int=i) for i in range(MAX_RELIED_ON + 1)]

    with pytest.raises(ValidationFailedError, match="at most"):
        validate_relied_on(ids, ids)


def test_action_is_an_identifier() -> None:
    assert normalize_action(" Credit.Approve_Increase ") == "credit.approve_increase"
    with pytest.raises(ValidationFailedError):
        normalize_action("approve increase!")


def test_rationale_and_agent_normalisation() -> None:
    assert normalize_rationale("  because  ") == "because"
    assert normalize_rationale("   ") is None
    assert normalize_rationale(None) is None
    assert normalize_agent(None) is None
    with pytest.raises(ValidationFailedError, match="rationale"):
        normalize_rationale("x" * (MAX_RATIONALE_CHARS + 1))


def test_outcome_must_be_json() -> None:
    assert validate_outcome({"approved": True}) == {"approved": True}
    with pytest.raises(ValidationFailedError):
        validate_outcome(None)
    with pytest.raises(ValidationFailedError):
        validate_outcome({"when": T})
