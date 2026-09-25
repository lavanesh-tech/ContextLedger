"""Retrieval metrics (pure functions).

For one query with a ranked list of retrieved ids, a set of relevant ids and a
set of forbidden ids:

* Recall@K    = |relevant in top K| / |relevant|
* Precision@K = |relevant in top K| / K          (K fixed, the usual definition)
* MRR         = 1 / rank of the first relevant result (0 if none)
* nDCG@K      = DCG@K / ideal DCG@K with binary relevance
* violation   = any forbidden id anywhere in the results

Ranking metrics are averaged over cases that have at least one relevant fact.
Correctness rates are over cases that define forbidden facts of that kind:
"temporal" (not valid at T, superseded, revoked) and "authorization" (another
tenant, above the caller's privacy ceiling).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

KS: Final = (1, 3, 5, 10)
TEMPORAL_REASONS: Final = frozenset({"not_valid_at_T", "superseded", "revoked"})
AUTHORIZATION_REASONS: Final = frozenset({"other_tenant", "privacy"})


def recall_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / k


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for position, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, item in enumerate(ranked[:k]) if item in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    case_id: str
    category: str
    ranked_keys: list[str]  # dataset keys of the retrieved facts, best first
    relevant: list[str]
    forbidden: dict[str, str]  # key -> reason
    latency_ms: float
    vector_search: str
    violations: list[str] = field(default_factory=list)  # forbidden keys that were retrieved


def outcome(
    *,
    case_id: str,
    category: str,
    ranked_keys: list[str],
    relevant: Iterable[str],
    forbidden: dict[str, str],
    latency_ms: float,
    vector_search: str,
) -> RetrievalOutcome:
    return RetrievalOutcome(
        case_id=case_id,
        category=category,
        ranked_keys=ranked_keys,
        relevant=sorted(relevant),
        forbidden=dict(forbidden),
        latency_ms=latency_ms,
        vector_search=vector_search,
        violations=[k for k in ranked_keys if k in forbidden],
    )


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _correctness(outcomes: list[RetrievalOutcome], reasons: frozenset[str]) -> float | None:
    scoped = [o for o in outcomes if any(r in reasons for r in o.forbidden.values())]
    clean = [o for o in scoped if not any(o.forbidden[v] in reasons for v in o.violations)]
    return round(len(clean) / len(scoped), 4) if scoped else None


def summarize(outcomes: list[RetrievalOutcome]) -> dict[str, object]:
    ranked = [o for o in outcomes if o.relevant]
    metrics: dict[str, object] = {
        "cases": len(outcomes),
        "cases_with_relevant_facts": len(ranked),
        "mrr": _mean([reciprocal_rank(o.ranked_keys, set(o.relevant)) for o in ranked]),
    }
    for k in KS:
        metrics[f"recall@{k}"] = _mean(
            [recall_at(o.ranked_keys, set(o.relevant), k) for o in ranked]
        )
        metrics[f"precision@{k}"] = _mean(
            [precision_at(o.ranked_keys, set(o.relevant), k) for o in ranked]
        )
        metrics[f"ndcg@{k}"] = _mean([ndcg_at(o.ranked_keys, set(o.relevant), k) for o in ranked])
    metrics["temporal_correctness"] = _correctness(outcomes, TEMPORAL_REASONS)
    metrics["authorization_correctness"] = _correctness(outcomes, AUTHORIZATION_REASONS)
    metrics["forbidden_results"] = sum(len(o.violations) for o in outcomes)
    latencies = sorted(o.latency_ms for o in outcomes)
    metrics["latency_ms_p50"] = latencies[len(latencies) // 2] if latencies else None
    metrics["by_category"] = {
        category: {
            "cases": len(group),
            "mrr": _mean(
                [reciprocal_rank(o.ranked_keys, set(o.relevant)) for o in group if o.relevant]
            ),
            "recall@3": _mean(
                [recall_at(o.ranked_keys, set(o.relevant), 3) for o in group if o.relevant]
            ),
            "forbidden_results": sum(len(o.violations) for o in group),
        }
        for category in sorted({o.category for o in outcomes})
        for group in [[o for o in outcomes if o.category == category]]
    }
    return metrics
