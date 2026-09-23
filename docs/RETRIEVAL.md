# Hybrid Temporal Retrieval

Phase 8 answers: *which facts are relevant to this question, as they were valid
at T and as ContextLedger knew them at K, for this actor?*

```python
result = await RetrievalService(session, provider).search(
    ctx,
    RetrievalQuery(
        query="What is the credit limit of customer-991?",
        valid_at=decision_time,  # default: now
        known_at=decision_time,  # default: everything recorded so far
        limit=10,
    ),
)
```

Relevance is statistical. Everything that decides what the caller may see and
which version is true is deterministic and enforced in SQL.

## Pipeline

```text
request ──► validate (pure) ──► facts:read? ──► embed question (no transaction held)
                                                   │ provider down → full text only,
                                                   │ result says vector_search="unavailable"
                                                   ▼
        ┌──────────── one REPEATABLE READ, READ ONLY transaction ────────────┐
        │ re-check facts:read, derive privacy scopes from the CURRENT role   │
        │                                                                    │
        │ hard filters (both branches):                                      │
        │   organization_id · valid at T as known at K · privacy scope       │
        │   · entity type / ids · properties · sources · min authority/conf. │
        │                                                                    │
        │ vector branch: fact_embeddings, cosine (HNSW), same model only ─┐  │
        │ text branch:   fact_search_documents, ts_rank_cd (GIN) ─────────┤  │
        │                          one statement (UNION ALL) ◄────────────┘  │
        │ load winners: version, entity, property, source                    │
        └────────────────────────────────────────────────────────────────────┘
                                                   ▼
        fuse: RRF + trust ──► top-k, each version shown as it looked at K
```

## Filters are pre-filters

The tenant, time and privacy conditions are inside each branch's `WHERE`
clause, not applied to the top-k afterwards. A post-filter can silently return
too few results, or none, when the nearest neighbours belong to the wrong time
or scope. pgvector 0.8's `hnsw.iterative_scan = relaxed_order` lets the HNSW
scan continue until enough rows pass the filters. The time condition is
`valid_at_condition`, the same SQL that Phase 5 checks against the pure-Python
reference model on random histories.

## Full-text search

Each fact version gets a search document in `fact_search_documents`, written by
an `AFTER INSERT` trigger on `fact_versions`. It is in the same transaction for
every write path (service, bulk SQL, migration backfill).

```text
customer customer-991 billing credit limit 5000
```

* Configuration `english`: stemming (`limits` → `limit`) and stop words, so
  "what is the credit limit" does not require the words "what", "is", "the".
* The question is parsed with `plainto_tsquery` (no query-syntax injection), and
  its terms are OR-combined. `ts_rank_cd` ranks documents that match more terms
  higher. An AND query would drop most facts for natural-language questions.
* Versions are searchable by full text immediately, even before the embedding
  worker has processed them.

## Ranking

Defined in `app/domain/retrieval.py` (pure and unit tested):

1. Each branch ranks its candidates (distance ascending, `ts_rank_cd` descending).
2. **Reciprocal Rank Fusion**: `rrf = Σ 1/(60 + rank)`. Only ranks are used, so a
   cosine distance and a text rank never have to be put on a common scale. A
   version found by both branches gets both terms.
3. **Trust**: `authority/100 × confidence`, recorded on each version from its source.
4. `score = rrf × ((1 − w) + w × trust)` with `trust_weight` `w` (default 0.3).
5. Ties are broken by version id, so results are deterministic.

Every result carries its breakdown (`vector_rank`, `vector_distance`,
`text_rank`, `text_score`, `rrf_score`, `trust`, `score`), so a decision receipt
(Phase 9) can record why a fact was in context.

Reranking with a cross-encoder or LLM is not implemented. Phase 18 measures
whether it would improve Recall@K / MRR enough to justify the latency and cost.

## Privacy and permissions

| Role | Sees privacy scopes |
|---|---|
| VIEWER | PUBLIC, INTERNAL |
| ENGINEER | + CONFIDENTIAL |
| ADMIN | + RESTRICTED |

A caller (for example an agent that must only see PUBLIC facts) can pass
`max_privacy_scope` to narrow this, never to widen it. The role is re-read
inside the retrieval transaction, so a role revoked a moment ago no longer
applies. Non-members are rejected before any embedding is paid for.

## Degraded mode

If the embedding provider fails, retrieval continues with full text only and
returns `vector_search="unavailable"`. Callers and receipts can see that the
answer had less recall, instead of getting an error or a silently different result.

## Measuring it

```bash
make bench-retrieval                             # 10,000 fact versions
make bench-retrieval BENCH_FACT_VERSIONS=100000
```

The benchmark (`benchmarks/scripts/retrieval_benchmark.py`) migrates a throwaway
database, bulk-loads a **synthetic benchmark dataset**, embeds it with the
offline provider, builds the HNSW index, and times `RetrievalService.search`
end to end: permission checks, query embedding, the hybrid SQL statement, fusion
and hydration. It records p50/p95/p99. Results are in [BENCHMARKS.md](BENCHMARKS.md).
Its "target in top-10" number is a sanity check, not a quality metric. Retrieval
quality is evaluated in Phase 18.
