# Revocation impact (Phase 17)

```text
FactVersion ──in──► ContextSnapshot ──used by──► Decision  ⇒  impact
```

Revoking a fact version says **it was wrong**, which is different from being
superseded ("it was true, then it changed"). Design decision: ADR-033 in
[DECISIONS.md](DECISIONS.md).

## Revoking

`POST /api/v1/organizations/{org}/facts/{fact_id}/revoke`
`{"reason": "...", "version_id": "<optional; default: latest version>"}`

In one transaction:

1. checks `facts:write` and `decisions:read`, and that you may see the version
   (a version above your privacy ceiling, or of another fact or tenant, is 404);
2. locks the fact, so it serializes with version writes, and refuses a second
   revocation of the same version (409);
3. stores the revocation (`fact_revocations`, append-only) and one
   `revocation_impacts` row per decision whose frozen context held the version,
   with `relied_on` when the decision cited it;
4. emits `fact.revoked` and `decision.impacted` (identifiers only) through the
   outbox triggers, and invalidates the retrieval cache.

The response is the impact report.

## What changes for readers

| Question | Before revocation at R | After |
|---|---|---|
| Current value / valid at T (latest knowledge) | the version | excluded |
| Valid at T **as known at K < R** | the version | still the version: that is what was known |
| Valid at T as known at K >= R | the version | excluded |
| Hybrid search, grounded answers, new decision contexts | included | excluded (same condition) |
| Fact history, lineage | listed | still listed; nothing is deleted |
| Decision receipts | shown | still shown, hash still verifies, fact marked `revoked_at` |

After revoking the latest version, the fact has no current value until a new
version is recorded (it must start later than the revoked one).

## Impact reports

- `GET …/facts/{fact_id}/impact`: one report per revoked version of the fact.
- `GET …/fact-versions/{version_id}/revocation`: one report.
- `GET …/revocations?limit=`: newest first.
- MCP `get_revocation_impact(fact_version_id)`.

Each report lists the decisions with `relied_on`, `recorded_at_revocation`
(it is in the immutable impact rows) and `decided_after_revocation` (recorded
later from a context captured before the revocation: the report is recomputed
on read, so these are not missed), plus counts.

Reading needs `facts:read` and `decisions:read`; a revocation of a version
above your privacy ceiling is not shown.

## Limits

- Impact is one hop: decisions whose context held the version. Downstream
  effects of those decisions are not modelled.
- The Neo4j provenance graph does not mark revoked versions yet.
