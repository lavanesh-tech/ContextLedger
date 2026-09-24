"""Evaluation dataset loading and metric calculations (no database, no model)."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.evaluation.dataset import EvalDataset, load_dataset
from app.evaluation.metrics import CaseOutcome, answer_metrics, pipeline_metrics
from app.evaluation.runner import DATASET, normalize_answer

CATEGORIES = {
    "temporal",
    "superseded",
    "insufficient_evidence",
    "tenant_isolation",
    "privacy",
    "contradiction",
    "citation",
}


def outcome(**overrides: Any) -> CaseOutcome:
    base: dict[str, Any] = {
        "case_id": "c",
        "category": "temporal",
        "expected_status": "answered",
        "status": "answered",
        "model_called": True,
        "context_has_expected": True,
        "context_leaks": [],
        "answer_matches": True,
        "citations_cover_expected": True,
        "rejected_citations": 0,
        "input_tokens": 100,
        "output_tokens": 20,
        "generation_ms": 10.0,
    }
    base.update(overrides)
    return CaseOutcome(**base)


# --- dataset ----------------------------------------------------------------------------------


def test_the_committed_dataset_is_valid_versioned_and_synthetic() -> None:
    dataset = load_dataset(DATASET)
    assert dataset.synthetic is True and dataset.version
    assert {c.category for c in dataset.cases} == CATEGORIES
    assert len(dataset.cases) >= 15


def test_every_answered_case_says_what_it_must_cite() -> None:
    for case in load_dataset(DATASET).cases:
        if case.expect.status == "answered":
            assert case.expect.cited_keys, case.id
            assert case.expect.answer_contains, case.id


def test_invalid_datasets_are_rejected(tmp_path: Path) -> None:
    raw = json.loads(DATASET.read_text())
    duplicate = {**raw, "cases": [raw["cases"][0], raw["cases"][0]]}
    with pytest.raises(ValidationError, match="unique"):
        EvalDataset.model_validate(duplicate)
    bad_key = json.loads(json.dumps(raw))
    bad_key["cases"][0]["expect"]["cited_keys"] = ["missing"]
    with pytest.raises(ValidationError, match="cited_keys"):
        EvalDataset.model_validate(bad_key)
    extra = json.loads(json.dumps(raw))
    extra["cases"][0]["surprise"] = 1
    with pytest.raises(ValidationError):
        EvalDataset.model_validate(extra)


# --- outcomes ----------------------------------------------------------------------------------


def test_a_case_is_correct_only_when_status_answer_and_citations_agree() -> None:
    assert outcome().correct
    assert not outcome(answer_matches=False).correct
    assert not outcome(citations_cover_expected=False).correct
    assert not outcome(status="ungrounded").correct
    assert outcome(expected_status="insufficient_evidence", status="insufficient_evidence").correct
    assert not outcome(expected_status="insufficient_evidence").correct


# --- metrics -------------------------------------------------------------------------------------


def test_pipeline_metrics() -> None:
    metrics = pipeline_metrics(
        [
            outcome(),
            outcome(context_has_expected=False),
            outcome(context_leaks=["5375", "Net 15"]),
            outcome(model_called=False, expected_status="insufficient_evidence"),
        ]
    )
    assert metrics == {
        "cases": 4,
        "model_calls": 3,
        "context_correctness": 0.5,
        "expected_facts_supplied": 0.6667,
        "forbidden_values_supplied": 2,
    }


def test_answer_metrics() -> None:
    outcomes = [
        outcome(case_id="a", generation_ms=100.0),
        outcome(case_id="b", category="citation", answer_matches=False, generation_ms=300.0),
        outcome(
            case_id="c",
            category="insufficient_evidence",
            expected_status="insufficient_evidence",
            status="insufficient_evidence",
            generation_ms=200.0,
        ),
        outcome(
            case_id="d",
            category="superseded",
            status="ungrounded",
            rejected_citations=1,
            citations_cover_expected=False,
            generation_ms=None,
        ),
        outcome(case_id="e", status="error", error="MalformedGenerationError", generation_ms=None),
    ]

    m = answer_metrics(outcomes, price_input_per_million=0.15, price_output_per_million=0.60)

    assert m["accuracy"] == 0.4
    assert m["accuracy_by_category"] == {
        "citation": 0.0,
        "insufficient_evidence": 1.0,
        "superseded": 0.0,
        "temporal": 0.5,
    }
    assert m["answered_case_accuracy"] == 0.25
    assert m["insufficient_evidence_accuracy"] == 1.0
    assert m["temporal_correctness"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["citation_validity"] == 0.8
    assert m["citation_recall"] == 1.0
    assert m["ungrounded_rate"] == 0.2
    assert m["structured_output_validity"] == 0.8
    assert m["errors"] == 1
    assert m["latency_ms"] == {"p50": 200.0, "p95": 300.0, "mean": 200.0}
    assert m["tokens"] == {"input": 500, "output": 100}
    assert m["estimated_cost_usd"] == pytest.approx(500 / 1e6 * 0.15 + 100 / 1e6 * 0.60)


def test_cost_is_not_estimated_without_prices() -> None:
    assert answer_metrics([outcome()])["estimated_cost_usd"] is None


def test_empty_inputs_do_not_divide_by_zero() -> None:
    assert pipeline_metrics([])["context_correctness"] is None
    assert answer_metrics([])["accuracy"] is None


def test_answers_are_normalized_before_matching() -> None:
    normalized = normalize_answer("The limit is $5,375.00 (was 2,150)")
    assert normalized == "the limit is $5375.00 (was 2150)"
    assert normalize_answer("Net 45, then Net 15") == "net 45, then net 15"
