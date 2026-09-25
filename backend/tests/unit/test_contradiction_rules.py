"""The deterministic contradiction rule (no database)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.contradictions import VersionFacts, preferred, value_conflict

T0 = datetime(2026, 1, 15, 8, tzinfo=UTC)
CRM, BILLING = UUID(int=1), UUID(int=2)


def version(
    n: int,
    source: UUID,
    value: Any,
    *,
    valid_from: int,
    observed: int | None = None,
    valid_until: int | None = None,
    authority: int = 50,
    confidence: str = "1.000",
) -> VersionFacts:
    return VersionFacts(
        id=UUID(int=100 + n),
        source_id=source,
        value=value,
        valid_from=T0 + timedelta(hours=valid_from),
        valid_until=None if valid_until is None else T0 + timedelta(hours=valid_until),
        observed_at=T0 + timedelta(hours=valid_from if observed is None else observed),
        authority=authority,
        confidence=Decimal(confidence),
    )


def test_a_new_claim_over_an_observed_instant_from_another_source_conflicts() -> None:
    crm = version(1, CRM, "ACTIVE", valid_from=0, observed=4)  # observed at 12:00
    billing = version(2, BILLING, "SUSPENDED", valid_from=2)  # claims from 10:00
    reason = value_conflict(crm, billing)
    assert reason is not None and "observed a different value" in reason


def test_an_ordinary_update_is_not_a_contradiction() -> None:
    crm = version(1, CRM, "ACTIVE", valid_from=0)  # observed at 08:00
    billing = version(2, BILLING, "SUSPENDED", valid_from=2)
    assert value_conflict(crm, billing) is None


def test_same_source_corrections_and_equal_values_are_not_contradictions() -> None:
    crm = version(1, CRM, "ACTIVE", valid_from=0, observed=4)
    assert value_conflict(crm, version(2, CRM, "SUSPENDED", valid_from=2)) is None
    assert value_conflict(crm, version(2, BILLING, "ACTIVE", valid_from=2)) is None


def test_an_observation_after_the_claimed_period_is_not_a_claim_about_it() -> None:
    crm = version(1, CRM, "ACTIVE", valid_from=0, valid_until=1, observed=4)
    assert value_conflict(crm, version(2, BILLING, "SUSPENDED", valid_from=2)) is None


def test_the_boundary_instant_counts_as_covered() -> None:
    crm = version(1, CRM, "ACTIVE", valid_from=0, observed=2)
    assert value_conflict(crm, version(2, BILLING, "SUSPENDED", valid_from=2)) is not None


def test_json_values_are_compared_by_value() -> None:
    crm = version(1, CRM, {"amount": 5000, "currency": "USD"}, valid_from=0, observed=4)
    same = version(2, BILLING, {"currency": "USD", "amount": 5000}, valid_from=2)
    assert value_conflict(crm, same) is None


def test_preference_is_authority_then_confidence_then_later_observation() -> None:
    low = version(1, CRM, "A", valid_from=0, observed=4, authority=60)
    high = version(2, BILLING, "B", valid_from=2, authority=90)
    assert preferred(low, high) == high.id
    sure = version(1, CRM, "A", valid_from=0, observed=4, confidence="0.900")
    unsure = version(2, BILLING, "B", valid_from=2, confidence="0.500")
    assert preferred(sure, unsure) == sure.id
    early = version(1, CRM, "A", valid_from=0, observed=1)
    late = version(2, BILLING, "B", valid_from=0, observed=3)
    assert preferred(early, late) == late.id
