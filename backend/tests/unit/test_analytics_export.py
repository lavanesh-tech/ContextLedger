"""Retrieval results -> flat, partitioned JSON Lines for Athena."""

import json
from pathlib import Path
from typing import Any

from app.evaluation.analytics_export import case_rows, column_name, export, run_id, run_rows

RESULT: dict[str, Any] = {
    "kind": "retrieval-evaluation",
    "date": "2026-09-25T00:55:49.606529+00:00",
    "commit_sha": "7c851db55fcc3206e9affaa3da010b19fcb9d93b",
    "working_tree_dirty": False,
    "dataset": {"name": "retrieval", "version": "1.0.0", "synthetic": True, "cases": 2},
    "configuration": {"embedding_model": "deterministic:hash-v1"},
    "metrics": {
        "text_only": {"cases": 2, "mrr": 0.5, "recall@5": 0.5, "by_category": {"x": {}}},
        "hybrid": {"cases": 2, "mrr": 1.0, "recall@5": 1.0, "temporal_correctness": 1.0},
    },
    "cases": {
        "hybrid": [
            {
                "case_id": "c1",
                "category": "lexical",
                "ranked_keys": ["a", "b"],
                "relevant": ["b"],
                "latency_ms": 3.2,
                "vector_search": "used",
                "violations": [],
            },
            {
                "case_id": "c2",
                "category": "privacy",
                "ranked_keys": ["z"],
                "relevant": [],
                "violations": ["forbidden z"],
            },
        ]
    },
}


def test_metric_names_become_sql_friendly_columns() -> None:
    assert column_name("recall@5") == "recall_at_5"
    assert column_name("ndcg@10") == "ndcg_at_10"
    assert column_name("mrr") == "mrr"


def test_run_id_combines_timestamp_and_commit() -> None:
    assert run_id(RESULT) == "20260925T005549-7c851db5"


def test_one_run_row_per_configuration_without_nested_breakdowns() -> None:
    rows = list(run_rows(RESULT))

    assert [row["configuration"] for row in rows] == ["hybrid", "text_only"]
    assert rows[0]["recall_at_5"] == 1.0
    assert rows[0]["synthetic"] is True
    assert "by_category" not in rows[1]


def test_case_rows_derive_ranks_from_the_recorded_results_only() -> None:
    first, second = case_rows(RESULT)

    assert first["first_relevant_rank"] == 2
    assert first["reciprocal_rank"] == 0.5
    assert second["first_relevant_rank"] is None
    assert second["reciprocal_rank"] == 0.0
    assert second["violation_count"] == 1


def test_export_writes_hive_partitions_and_skips_other_result_kinds(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "retrieval-x.json").write_text(json.dumps(RESULT))
    (results / "answers-y.json").write_text(json.dumps({"kind": "answer-evaluation"}))

    files = export(results, tmp_path / "out")

    assert sorted(p.relative_to(tmp_path / "out").as_posix() for p in files) == [
        "retrieval_cases/run_date=2026-09-25/20260925T005549-7c851db5.jsonl",
        "retrieval_runs/run_date=2026-09-25/20260925T005549-7c851db5.jsonl",
    ]
    lines = (tmp_path / "out/retrieval_runs/run_date=2026-09-25").glob("*.jsonl")
    assert len(next(lines).read_text().splitlines()) == 2
