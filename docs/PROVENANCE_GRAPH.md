# Provenance Graph (Neo4j)

PostgreSQL is the system of record. Neo4j holds a **projection** of the
provenance relationships, so questions that are multi-hop traversals stay
short and fast:

* *If this source turns out to be wrong, which decisions are affected?*
* *Which decisions relied on this fact version, or merely had it in context?*
* *What evidence and sources does this decision ultimately rest on?*

## Graph model

```text
(Entity)<-[:OF_ENTITY]-(Fact)<-[:VERSION_OF]-(FactVersion)-[:ASSERTED_BY]->(Source)
                                                 ▲   │                         ▲
                                  [:SUPERSEDES]──┘   │                         │
                                                     │               [:FROM_SOURCE]
(Evidence)-[:EVIDENCE_FOR {relation, linked_at}]─────┘                 (Evidence)
(Decision)-[:RELIED_ON]->(FactVersion)
(Decision)-[:BASED_ON]->(Snapshot)-[:INCLUDED {position}]->(FactVersion)
```

Every node has `id` (the PostgreSQL UUID, unique per label) and `org` (the
tenant, indexed). The graph holds identities, timestamps, privacy scopes and
hashes. It **never** holds fact values or evidence text, which stay in
PostgreSQL behind its tenant and privacy rules.

## How changes reach the graph

```text
INSERT/UPDATE on entities, facts, fact_sources, fact_versions, evidence,
fact_version_evidence, context_snapshots, context_snapshot_facts,
decisions, decision_facts
        │  AFTER trigger, same transaction
        ▼
graph_outbox (organization_id, table_name, row_key)
        │  graph-projector: claim with FOR UPDATE SKIP LOCKED
        ▼
re-read current rows ──► one Neo4j transaction of idempotent MERGEs ──► delete events, commit
```

* **Nothing is missed.** The outbox row is written by a trigger in the same
  transaction as the change, whichever code path made the change.
* **Nothing is double-counted.** `MERGE` by id makes every event idempotent. A
  crash between the Neo4j write and the PostgreSQL commit only means the event
  is applied again.
* **Order does not matter.** A relationship to a node that is not projected
  yet creates a placeholder (`id`, `org`), which that node's own event fills in.
  Events carry keys, not data, so the projector always writes the *current* row.
* **Failures are safe.** If Neo4j is down, the PostgreSQL transaction rolls back
  and the events stay queued. A test checks this.
* **Rebuildable.** Migration 0008 backfills the outbox from existing rows. The
  same statement per table can re-project a tenant, or the whole graph, at any time.

The projector runs as the Compose service `graph-projector`, or with
`make graph-projector` / `make graph-once` from your Mac.

## Queries (`ProvenanceService`)

| Method | Answers |
|---|---|
| `impact_of_fact_version` | decisions that relied on it, decisions that had it in context only, and later versions that superseded it |
| `impact_of_source` | versions the source asserted, versions supported by its evidence, and every decision relying on any of them, with the reason for each |
| `impact_of_evidence` | versions the evidence supports or contradicts, and the decisions relying on them |
| `decision_lineage` | for each relied-on version: entity, property, asserting source and linked evidence |

Authorization (`decisions:read`) and tenancy are checked in PostgreSQL first.
Every Cypher query then anchors on `{id, org}` and only follows nodes of the same
`org`. Lineage steps above the reader's privacy ceiling are redacted.

The graph is eventually consistent. Every answer includes `pending_events`, the
number of this organization's changes not yet projected, so a caller can tell
"no impact" apart from "not projected yet".
