"""Retrieval metric definitions, checked by hand-computed examples."""

import math
from pathlib import Path

import pytest

from app.evaluation.retrieval_eval import DATASET, load_retrieval_dataset
from app.evaluation.retrieval_metrics import (
    ndcg_at,
    outcome,
    precision_at,
    recall_at,
    reciprocal_rank,
    summarize,
)


def test_recall_precision_and_reciprocal_rank() -> None:
    ranked = ["x", "a", "y", "b"]
    relevant = {"a", "b"}
    assert recall_at(ranked, relevant, 1) == 0.0
    assert recall_at(ranked, relevant, 2) == 0.5
    assert recall_at(ranked, relevant, 4) == 1.0
    assert precision_at(ranked, relevant, 2) == 0.5
    assert precision_at(ranked, relevant, 10) == 0.2  # K is fixed, even past the list end
    assert reciprocal_rank(ranked, relevant) == 0.5
    assert reciprocal_rank(["x"], relevant) == 0.0


def test_ndcg() -> None:
    assert ndcg_at(["a", "b"], {"a", "b"}, 2) == 1.0
    expected = (1 / math.log2(3)) / (1 + 1 / math.log2(3))
    assert ndcg_at(["x", "a"], {"a", "b"}, 2) == pytest.approx(expected)


def test_summary_separates_ranking_from_correctness() -> None:
    outcomes = [
        outcome(
            case_id="a",
            category="lexical",
            ranked_keys=["hit", "other"],
            relevant=["hit"],
            forbidden={},
            latency_ms=5,
            vector_search="used",
        ),
        outcome(
            case_id="b",
            category="temporal",
            ranked_keys=["old", "new"],
            relevant=["new"],
            forbidden={"old": "superseded"},
            latency_ms=7,
            vector_search="used",
        ),
        outcome(
            case_id="c",
            category="tenant_isolation",
            ranked_keys=[],
            relevant=[],
            forbidden={"theirs": "other_tenant"},
            latency_ms=3,
            vector_search="used",
        ),
    ]
    metrics = summarize(outcomes)
    assert metrics["cases_with_relevant_facts"] == 2
    assert metrics["mrr"] == 0.75
    assert metrics["recall@1"] == 0.5
    assert metrics["temporal_correctness"] == 0.0  # the superseded version was retrieved
    assert metrics["authorization_correctness"] == 1.0
    assert metrics["forbidden_results"] == 1
    assert outcomes[1].violations == ["old"]


def test_the_committed_dataset_is_valid() -> None:
    dataset = load_retrieval_dataset(Path(DATASET))
    assert dataset.synthetic is True
    assert len(dataset.cases) >= 20
    categories = {c.category for c in dataset.cases}
    assert {"temporal", "revoked", "tenant_isolation", "privacy", "trust"} <= categories
