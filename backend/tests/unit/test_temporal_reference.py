"""Bitemporal semantics, with fully controlled valid time AND transaction time.

Scenario used throughout (the spec's credit-limit example, plus late knowledge):

    v1  2000  valid [10:30, 14:15)  recorded 10:31, end learned 16:00
    v2  5000  valid [14:15, ∞)      recorded 16:00  (the billing system synced late)
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.errors import InvariantViolationError, ValidationFailedError
from app.domain.facts import PrivacyScope
from app.temporal.model import VersionSnapshot
from app.temporal.reference import (
    as_known,
    change_between,
    history_as_known,
    is_valid_at,
    resolve,
)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=UTC)


FACT = uuid4()
US = timedelta(microseconds=1)


def version(
    number: int,
    value: object,
    valid_from: datetime,
    recorded_at: datetime,
    valid_until: datetime | None = None,
    valid_until_recorded_at: datetime | None = None,
    supersedes: VersionSnapshot | None = None,
) -> VersionSnapshot:
    return VersionSnapshot(
        id=uuid4(),
        fact_id=FACT,
        version=number,
        value=value,
        source_id=uuid4(),
        valid_from=valid_from,
        valid_until=valid_until,
        observed_at=valid_from,
        recorded_at=recorded_at,
        valid_until_recorded_at=valid_until_recorded_at,
        supersedes_id=supersedes.id if supersedes else None,
        authority=90,
        confidence=Decimal("1.000"),
        privacy_scope=PrivacyScope.INTERNAL,
    )


V1 = version(
    1, 2000, at(10, 30), at(10, 31), valid_until=at(14, 15), valid_until_recorded_at=at(16)
)
V2 = version(2, 5000, at(14, 15), at(16), supersedes=V1)
HISTORY = [V2, V1]  # deliberately unordered


# --- "current value" vs "what did the agent know" -------------------------------------


def test_current_value_is_version_2() -> None:
    assert resolve(HISTORY, valid_at=at(17), known_at=None) == V2


def test_value_valid_at_11_with_todays_knowledge_is_version_1() -> None:
    assert resolve(HISTORY, valid_at=at(11), known_at=None) == V1


def test_what_the_agent_knew_at_11_is_version_1_still_open_ended() -> None:
    known = resolve(HISTORY, valid_at=at(11), known_at=at(11))

    assert known is not None
    assert known.id == V1.id
    assert known.valid_until is None  # at 11:00 nobody knew it would end at 14:15


def test_late_knowledge_agent_at_1530_still_believed_2000() -> None:
    # True value at 15:30 was 5000, but ContextLedger only learned it at 16:00.
    believed = resolve(HISTORY, valid_at=at(15, 30), known_at=at(15, 30))
    actual = resolve(HISTORY, valid_at=at(15, 30), known_at=None)

    assert believed is not None and believed.value == 2000
    assert actual is not None and actual.value == 5000


def test_nothing_was_known_before_the_first_recording() -> None:
    assert resolve(HISTORY, valid_at=at(11), known_at=at(10, 31) - US) is None


def test_knowledge_boundaries_are_inclusive() -> None:
    assert resolve(HISTORY, valid_at=at(11), known_at=at(10, 31)) is not None
    new_value = resolve(HISTORY, valid_at=at(15), known_at=at(16))
    assert new_value is not None and new_value.value == 5000


def test_nothing_is_valid_before_the_first_valid_from() -> None:
    assert resolve(HISTORY, valid_at=at(10, 30) - US, known_at=None) is None


def test_valid_time_boundaries_are_half_open() -> None:
    assert resolve(HISTORY, valid_at=at(14, 15) - US, known_at=None) == V1
    assert resolve(HISTORY, valid_at=at(14, 15), known_at=None) == V2


# --- history and snapshots ---------------------------------------------------------------


def test_history_as_known_is_ordered_and_masks_unknown_ends() -> None:
    at_noon = history_as_known(HISTORY, at(12))

    assert [v.version for v in at_noon] == [1]
    assert at_noon[0].valid_until is None
    assert [v.version for v in history_as_known(HISTORY, None)] == [1, 2]


def test_as_known_keeps_an_end_that_was_already_known() -> None:
    assert as_known(V1, at(16)) == V1
    assert as_known(V1, None) == V1


def test_is_valid_at_open_ended() -> None:
    assert is_valid_at(V2, at(23, 59))


def test_naive_times_are_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        resolve(HISTORY, valid_at=datetime(2026, 9, 23, 11), known_at=None)  # noqa: DTZ001
    with pytest.raises(ValidationFailedError):
        history_as_known(HISTORY, datetime(2026, 9, 23, 11))  # noqa: DTZ001


def test_overlapping_versions_are_reported_not_silently_resolved() -> None:
    broken = replace(V2, valid_from=at(12))  # overlaps V1 at 13:00 (impossible in the DB)

    with pytest.raises(InvariantViolationError):
        resolve([V1, broken], valid_at=at(13), known_at=None)


# --- changes between T1 and T2 ------------------------------------------------------------


def test_change_across_the_update() -> None:
    change = change_between(HISTORY, at(11), at(15), known_at=None)

    assert change is not None
    before, after, transitions = change
    assert (before, after) == (V1, V2)
    assert transitions == (V2,)


def test_no_change_inside_one_version() -> None:
    assert change_between(HISTORY, at(11), at(12), known_at=None) is None


def test_change_as_known_before_the_sync_shows_nothing() -> None:
    assert change_between(HISTORY, at(11), at(15), known_at=at(15, 30)) is None


def test_short_lived_version_inside_the_window_is_reported() -> None:
    temp = version(1, "PROMO", at(12), at(12), valid_until=at(13), valid_until_recorded_at=at(12))

    change = change_between([temp], at(11), at(14), known_at=None)

    assert change == (None, None, (temp,))


def test_expiring_version_is_a_change() -> None:
    change = change_between([V1], at(11), at(15), known_at=None)

    assert change == (V1, None, ())


@pytest.mark.parametrize("end", [at(11), at(10)])
def test_change_window_must_move_forward(end: datetime) -> None:
    with pytest.raises(ValidationFailedError):
        change_between(HISTORY, at(11), end, known_at=None)
