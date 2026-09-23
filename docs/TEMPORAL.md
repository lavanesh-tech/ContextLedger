# Temporal Semantics

ContextLedger stores facts **bitemporally** and answers time questions
**deterministically**. This page is the contract. The reference implementation is
`backend/app/temporal/reference.py`, the SQL implementation is
`backend/app/repositories/temporal.py`, and a differential test checks that they
agree on randomly generated histories.

## Two timelines

| Timeline | Columns | Question it answers | Set by |
|---|---|---|---|
| **Valid time** | `valid_from`, `valid_until` | When is the value true in the world? | the caller (source) |
| **Transaction time** | `recorded_at`, `valid_until_recorded_at` | When did ContextLedger learn it? | the database clock |

Valid time is half-open, `[valid_from, valid_until)`. `valid_until = NULL` means
"until further notice".

## Definitions

For a version `v`, a valid-time instant `T` and a knowledge instant `K`:

- **known at K**: `v.recorded_at <= K`
- **end known at K**: `v.valid_until_recorded_at <= K`. Before that moment, `v`
  looked open-ended, and snapshots "as known at K" show `valid_until = NULL`.
- **valid at T as known at K**: known at K, `v.valid_from <= T`, and
  (`v.valid_until IS NULL` or its end is not known at K or `T < v.valid_until`)
- **resolve(T, K)**: the unique version of a fact valid at T as known at K, or
  nothing. `K = latest` means "everything recorded so far".

Uniqueness is guaranteed by the database:
- an `EXCLUDE` constraint prevents overlapping valid time within a fact;
- the versions known at any K form a prefix of the version chain, because
  `recorded_at` is strictly increasing per fact (taken with `clock_timestamp()`
  while holding the fact's row lock);
- a version's end is recorded at the same instant as its successor.

## The questions, answered

Credit limit of `customer-991`:

```text
v1  2000  valid [10:30, 14:15)  recorded 10:31, end learned 16:00
v2  5000  valid [14:15, ∞)      recorded 16:00   (billing synced late)
```

| Question | Call | Answer |
|---|---|---|
| What is the credit limit now? | `facts_at(valid_at=now)` | v2 = 5000 |
| What was it at 11:00 (with today's knowledge)? | `facts_at(valid_at=11:00)` | v1 = 2000 |
| What did the agent know when it decided at 11:00? | `facts_at(valid_at=11:00, known_at=11:00)` | v1 = 2000, shown open-ended |
| What did an agent deciding at 15:30 believe? | `facts_at(valid_at=15:30, known_at=15:30)` | v1 = 2000, although the true value was 5000 |
| What changed between 11:00 and 15:00? | `changes_between(11:00, 15:00)` | credit_limit: v1 → v2 |
| Which version superseded v1? | `lineage(v1)` | v2 |

The fourth row is why a single timeline is not enough: without transaction time,
ContextLedger could not show that the agent acted correctly on what it knew.

## Rules for writing

1. Versions are never edited or deleted (database trigger).
2. A new version must start after the latest one. An open latest version is
   closed at the new start; a fixed-end version allows gaps but not overlaps.
3. Changing the past is a **revocation** (Phase 17), recorded as new knowledge,
   so earlier answers stay reproducible.
