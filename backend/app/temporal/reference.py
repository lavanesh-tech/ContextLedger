"""Reference implementation of ContextLedger's bitemporal semantics.

These pure functions define *what the right answer is*. The SQL engine
(app/repositories/temporal.py) must return exactly the same answers; an
integration test compares both on randomly generated histories.

Definitions (see docs/TEMPORAL.md):

* A version is **known at K** when ``recorded_at <= K``.
  ``known_at=None`` means "everything recorded so far".
* Its end is **known at K** when ``valid_until_recorded_at <= K``. Before that,
  the version looked open-ended, and a snapshot "as known at K" shows it that way.
* A version **is valid at T** when ``valid_from <= T < valid_until``
  (half-open; no ``valid_until`` means "until further notice").
* **Resolve(T, K)**: the single version valid at T as known at K, or None.
"""

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime

from app.domain.errors import InvariantViolationError, ValidationFailedError
from app.domain.facts import require_aware
from app.temporal.model import VersionSnapshot


def is_known(version: VersionSnapshot, known_at: datetime | None) -> bool:
    return known_at is None or version.recorded_at <= known_at


def as_known(version: VersionSnapshot, known_at: datetime | None) -> VersionSnapshot:
    """The version as it looked at ``known_at`` (hides an end not yet learned)."""
    closed_at = version.valid_until_recorded_at
    if known_at is None or closed_at is None or closed_at <= known_at:
        return version
    return replace(version, valid_until=None, valid_until_recorded_at=None)


def is_valid_at(version: VersionSnapshot, instant: datetime) -> bool:
    return version.valid_from <= instant and (
        version.valid_until is None or instant < version.valid_until
    )


def history_as_known(
    versions: Iterable[VersionSnapshot], known_at: datetime | None
) -> list[VersionSnapshot]:
    """All versions known at ``known_at``, oldest first, each as it looked then."""
    if known_at is not None:
        require_aware(known_at, field="known_at")
    return [
        as_known(v, known_at)
        for v in sorted(versions, key=lambda v: v.version)
        if is_known(v, known_at)
    ]


def resolve(
    versions: Iterable[VersionSnapshot], valid_at: datetime, known_at: datetime | None
) -> VersionSnapshot | None:
    """The version of one fact valid at ``valid_at``, as known at ``known_at``."""
    require_aware(valid_at, field="valid_at")
    matches = [v for v in history_as_known(versions, known_at) if is_valid_at(v, valid_at)]
    if len(matches) > 1:  # impossible while the database constraints hold
        raise InvariantViolationError(
            f"{len(matches)} versions of one fact are valid at {valid_at.isoformat()}"
        )
    return matches[0] if matches else None


def change_between(
    versions: Sequence[VersionSnapshot],
    start: datetime,
    end: datetime,
    known_at: datetime | None,
) -> tuple[VersionSnapshot | None, VersionSnapshot | None, tuple[VersionSnapshot, ...]] | None:
    """(before, after, transitions) for one fact, or None if nothing changed."""
    require_aware(start, field="start")
    require_aware(end, field="end")
    if end <= start:
        raise ValidationFailedError("end must be later than start")

    known = history_as_known(versions, known_at)
    before = resolve(known, start, None)
    after = resolve(known, end, None)
    transitions = tuple(v for v in known if start < v.valid_from <= end)

    before_id = before.id if before else None
    after_id = after.id if after else None
    if before_id == after_id and not transitions:
        return None
    return before, after, transitions
