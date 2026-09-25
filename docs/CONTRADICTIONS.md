# Contradiction detection (Phase 16)

Sources disagree. ContextLedger records the disagreement instead of letting
the newer write silently win, keeps both versions, and leaves the decision to a
person. Design decision: ADR-032 in [DECISIONS.md](DECISIONS.md).

## The deterministic rule

Each fact (one property of one entity) has a single version chain; a new
version supersedes the previous one. Usually that is a real change. It is a
contradiction under rule **`observed-value-conflict-v1`** when all of these hold:

1. the new version N and the previous version P come from **different sources**;
2. their **values differ** (JSON equality: `{"a":1,"b":2}` equals `{"b":2,"a":1}`);
3. N is valid from an instant **at or before P's `observed_at`**, the moment P's
   source directly observed its value (and that observation lay inside P's
   claimed validity).

```text
CRM      customer_status = ACTIVE     valid from 08:00, observed at 12:00
Billing  customer_status = SUSPENDED  valid from 10:00
                                     → at 12:00 the two sources disagree
```

Not contradictions: an update from a later instant than anything observed
(`observed_at` defaults to `valid_from`), a correction from the same source,
the same value from another source.

The rule runs inside `FactService.record_version`, in the same transaction as
the write. A contradiction row and a `contradiction.detected` event (identifiers
only, no values) are committed together with the version, or not at all.

Code: `app/domain/contradictions.py` (pure rule), `app/services/facts.py`
(detection on write), `app/services/contradictions.py` (reading and resolving).

## What is recorded

| Field | Meaning |
|---|---|
| `left`, `right` | Both versions (earlier, later) with property, source and full snapshot |
| `kind` | `value_conflict` (rule, same fact) or `semantic` (LLM suggestion, different facts) |
| `detector` | `observed-value-conflict-v1`, or `llm:contradiction-review-v1` |
| `preferred_version_id` | The rules' preference: higher authority, then confidence, then later observation. Null for LLM suggestions |
| `privacy_scope` | The more sensitive scope of the two versions |
| `status` | `open`, then `resolved` or `dismissed` by a person, with note, user and time |

Nothing is ever deleted, and resolving a contradiction does not change any
fact. To correct data, record a new version (or, from Phase 17, revoke one).

## Access

- Listing and reading need `facts:read`. A contradiction is shown only if the
  reader may see **both** versions; otherwise it is invisible (404 by id).
- Resolving needs `facts:write`, works once (then 409), and cannot reopen.
- The MCP tool `get_contradictions` applies the agent's privacy ceiling too.

## Optional LLM review

`POST …/entities/{type}/{external_id}/contradiction-review` asks the model
for conflicts between **different** properties (for example
`customer_status = ACTIVE` and `account_closed = true`), which the rule cannot
see.

- Only the entity's current facts the caller may see are sent, labelled F1..Fn.
- Prompt `contradiction-review-v1`, strict JSON schema output, through the same
  LangChain adapter and provider as the other AI features.
- A finding naming an unknown label, or the same fact twice, is rejected and
  counted (`rejected_findings`). Accepted findings become open `semantic`
  contradictions; a pair already recorded is not duplicated (`already_known`).
- Needs `facts:read` and `facts:write`; 503 when generation is disabled or fails.

The review's precision has not been measured; treat its output as suggestions.

## Limits

- A source that does not send `observed_at` never triggers the rule.
- No numeric tolerance (4999.99 vs 5000 is a conflict) and no cross-entity rules.
