from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.domain.errors import ConflictError, ValidationFailedError
from app.domain.facts import LatestVersion, ValidityWindow, VersionPlan, plan_new_version

T0 = datetime(2026, 9, 23, 10, 30, tzinfo=UTC)
HOUR = timedelta(hours=1)


def latest(version: int, start: datetime, end: datetime | None = None) -> LatestVersion:
    return LatestVersion(version=version, window=ValidityWindow(start, end))


# --- ValidityWindow ---------------------------------------------------------------


def test_window_is_half_open() -> None:
    window = ValidityWindow(T0, T0 + HOUR)

    assert window.contains(T0)
    assert window.contains(T0 + HOUR - timedelta(microseconds=1))
    assert not window.contains(T0 + HOUR)
    assert not window.contains(T0 - timedelta(microseconds=1))


def test_open_ended_window_contains_the_far_future() -> None:
    assert ValidityWindow(T0).contains(T0 + timedelta(days=36500))


def test_window_compares_instants_across_timezones() -> None:
    eastern = timezone(timedelta(hours=-4))

    assert ValidityWindow(T0, T0 + HOUR).contains(datetime(2026, 9, 23, 7, 0, tzinfo=eastern))


@pytest.mark.parametrize("end", [T0, T0 - HOUR])
def test_window_end_must_be_after_start(end: datetime) -> None:
    with pytest.raises(ValidationFailedError, match="valid_until"):
        ValidityWindow(T0, end)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationFailedError, match="timezone"):
        ValidityWindow(datetime(2026, 9, 23, 10, 30))  # noqa: DTZ001 (deliberately naive)


# --- plan_new_version -------------------------------------------------------------


def test_first_version_is_number_one_and_closes_nothing() -> None:
    assert plan_new_version(None, ValidityWindow(T0)) == VersionPlan(1, None)


def test_credit_limit_example_closes_the_open_version_at_the_new_start() -> None:
    # v18 valid from 10:30 (open); v19 arrives valid from 14:15.
    change_at = T0 + timedelta(hours=3, minutes=45)

    plan = plan_new_version(latest(18, T0), ValidityWindow(change_at))

    assert plan == VersionPlan(version=19, close_previous_at=change_at)


@pytest.mark.parametrize("start", [T0, T0 - HOUR])
def test_new_version_cannot_start_at_or_before_the_latest(start: datetime) -> None:
    with pytest.raises(ConflictError, match="revocation"):
        plan_new_version(latest(3, T0), ValidityWindow(start))


def test_gap_after_a_fixed_end_is_allowed_without_closing_anything() -> None:
    plan = plan_new_version(latest(1, T0, T0 + HOUR), ValidityWindow(T0 + 2 * HOUR))

    assert plan == VersionPlan(version=2, close_previous_at=None)


def test_new_version_may_start_exactly_when_a_fixed_window_ends() -> None:
    plan = plan_new_version(latest(1, T0, T0 + HOUR), ValidityWindow(T0 + HOUR))

    assert plan == VersionPlan(version=2, close_previous_at=None)


def test_new_version_cannot_overlap_a_fixed_window() -> None:
    with pytest.raises(ConflictError, match="overlaps"):
        plan_new_version(latest(1, T0, T0 + 2 * HOUR), ValidityWindow(T0 + HOUR))
