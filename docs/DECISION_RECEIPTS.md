# Decision Receipts

A decision receipt answers, long after the fact: **what was decided, by whom or
which agent, when, and which facts, exactly as they were known at that moment,
did the decision rely on?**

```text
capture_context(query)                     record_decision(snapshot, relied_on)
        │                                              │
        ▼                                              ▼
ContextSnapshot  (query, valid_at, known_at,   Decision (action, outcome, rationale,
                  model, privacy scopes,                 agent, decided_at,
                  parameters)                            receipt_sha256)
        │ 1..n                                         │ 0..n
        ▼                                              ▼
ContextSnapshotFact (position, score breakdown) ◄── DecisionFact (relied on)
        │
        ▼
FactVersion ── Source ── Evidence (linked by known_at)
```

## Lifecycle

1. **Capture.** `DecisionService.capture_context` pins `known_at` to the database
   clock (or to an explicit instant, to reconstruct the past). `valid_at`
   defaults to the same instant. It runs hybrid retrieval with both pinned
   (see [RETRIEVAL.md](RETRIEVAL.md)) and stores the ranked result with each
   fact's score breakdown.
2. **Decide.** `record_decision` stores the action (an identifier such as
   `credit.approve_increase`), the outcome (JSON), an optional rationale and agent
   label, and the subset of snapshot facts the decision relied on. It computes
   the receipt hash in the same transaction.
3. **Prove.** `receipt` returns the decision, the frozen context and every fact
   *as known at `known_at`*: a later correction or supersession does not change
   what the receipt shows. It includes the source and the evidence linked by
   then, and recomputes the hash.

## Guarantees and where they are enforced

| Guarantee | Enforced by |
|---|---|
| Receipts never change | Append-only triggers on all four tables |
| A decision can only cite facts that were in its context | Composite FK `(snapshot_id, fact_version_id)` → `context_snapshot_facts`, plus a service-level check with a clear error |
| Everything stays inside one tenant | Composite `(organization_id, …)` foreign keys on every table |
| Later knowledge does not leak into old receipts | Facts are shown with `as_known(version, known_at)`; evidence linked after `known_at` is excluded |
| Tampering is detectable | `receipt_sha256` over a canonical document, recomputed on every read (`integrity_verified`) |

The hash covers the decision fields, the context (query, instants, model,
vector-search status, privacy scopes) and every snapshot fact's version id,
position, value hash and relied-on flag. The document is canonical JSON (sorted
keys, no whitespace, UTF-8, UTC ISO-8601 timestamps, schema
`contextledger.receipt.v1`), so any independent implementation can reproduce it.
A test disables the trigger as the table owner, edits an outcome, and checks
that the receipt reports `integrity_verified = False`.

A hash alone does not stop someone who can rewrite both a row and its hash.
Signing receipts with a key held outside the database (e.g. AWS KMS) is
tracked for the security review (Phase 28).

## Permissions and privacy

* Capturing context and recording decisions need `decisions:record` (ENGINEER,
  ADMIN). Reading receipts needs `decisions:read` (all roles).
* A reader sees facts up to their role's privacy ceiling. Facts above it appear
  as `redacted=True`, with their id, position, scope and relied-on flag but no
  content. The receipt still verifies, because redaction happens after hashing.

## Impact queries

`decisions_relying_on(fact_version_id)` lists the decisions that relied on a
version. Phase 17 (revocation impact) builds on it: when a fact is found to be
wrong, find every decision that used it.
