"""Metrics over evaluated cases. Pure functions: no I/O, fully unit-tested.

Two families, reported separately and never mixed:

* **Pipeline metrics** (deterministic, meaningful with any model): did the model
  receive the right fact versions, and nothing it must not see?
* **Answer metrics** (meaningful only with a real model): was the answer right,
  properly cited, correctly declined, valid structured output?
"""

import math
import statistics
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    category: str
    expected_status: str
    status: str  # answered | insufficient_evidence | ungrounded | error
    model_called: bool
    context_has_expected: bool  # every expected cited fact was supplied to the model
    context_leaks: list[str]  # forbidden values found in what the model received
    answer_matches: bool  # contains / excludes checks on the answer text
    citations_cover_expected: bool
    rejected_citations: int
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    generation_ms: float | None = None
    answer: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def correct(self) -> bool:
        if self.status != self.expected_status:
            return False
        if self.expected_status == "insufficient_evidence":
            return True
        return self.answer_matches and self.citations_cover_expected


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return round(ordered[index], 2)


def pipeline_metrics(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    supplied = [o for o in outcomes if o.model_called]
    expects_facts = [o for o in outcomes if o.model_called and o.expected_status == "answered"]
    leaks = sum(len(o.context_leaks) for o in outcomes)
    return {
        "cases": len(outcomes),
        "model_calls": len(supplied),
        "context_correctness": _rate(
            sum(o.context_has_expected and not o.context_leaks for o in outcomes), len(outcomes)
        ),
        "expected_facts_supplied": _rate(
            sum(o.context_has_expected for o in expects_facts), len(expects_facts)
        ),
        "forbidden_values_supplied": leaks,
    }


def answer_metrics(
    outcomes: list[CaseOutcome],
    *,
    price_input_per_million: float | None = None,
    price_output_per_million: float | None = None,
) -> dict[str, Any]:
    by_category: dict[str, list[CaseOutcome]] = {}
    for o in outcomes:
        by_category.setdefault(o.category, []).append(o)
    generated = [o for o in outcomes if o.model_called]
    answered = [o for o in outcomes if o.status == "answered"]
    expected_insufficient = [o for o in outcomes if o.expected_status == "insufficient_evidence"]
    expected_answer = [o for o in outcomes if o.expected_status == "answered"]
    latencies = [o.generation_ms for o in generated if o.generation_ms is not None]
    input_tokens = sum(o.input_tokens for o in outcomes)
    output_tokens = sum(o.output_tokens for o in outcomes)
    cost = None
    if price_input_per_million is not None and price_output_per_million is not None:
        cost = round(
            input_tokens / 1e6 * price_input_per_million
            + output_tokens / 1e6 * price_output_per_million,
            6,
        )
    return {
        "accuracy": _rate(sum(o.correct for o in outcomes), len(outcomes)),
        "accuracy_by_category": {
            category: _rate(sum(o.correct for o in items), len(items))
            for category, items in sorted(by_category.items())
        },
        "answered_case_accuracy": _rate(
            sum(o.correct for o in expected_answer), len(expected_answer)
        ),
        "insufficient_evidence_accuracy": _rate(
            sum(o.status == "insufficient_evidence" for o in expected_insufficient),
            len(expected_insufficient),
        ),
        "temporal_correctness": _rate(
            sum(o.correct for o in outcomes if o.category in {"temporal", "superseded"}),
            sum(o.category in {"temporal", "superseded"} for o in outcomes),
        ),
        "citation_validity": _rate(
            sum(o.rejected_citations == 0 for o in generated), len(generated)
        ),
        "citation_recall": _rate(
            sum(o.citations_cover_expected for o in answered if o.expected_status == "answered"),
            sum(o.expected_status == "answered" for o in answered),
        ),
        "ungrounded_rate": _rate(sum(o.status == "ungrounded" for o in generated), len(generated)),
        "structured_output_validity": _rate(
            sum(o.error != "MalformedGenerationError" for o in generated), len(generated)
        ),
        "errors": sum(o.status == "error" for o in outcomes),
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "mean": round(statistics.fmean(latencies), 2) if latencies else None,
        },
        "tokens": {"input": input_tokens, "output": output_tokens},
        "estimated_cost_usd": cost,
    }
