"""Export retrieval-evaluation results as Athena-friendly JSON Lines (Phase 26).

    make analytics-export              # evaluation/results -> evaluation/analytics
    make analytics-upload              # aws s3 sync to the analytics bucket

Each committed ``retrieval-*.json`` result becomes two flat row sets, partitioned by
run date (Hive layout, matched by Athena partition projection):

* ``retrieval_runs/run_date=YYYY-MM-DD/<run_id>.jsonl``: one row per configuration
  (hybrid, hybrid_no_trust, text_only) with the aggregate metrics.
* ``retrieval_cases/run_date=YYYY-MM-DD/<run_id>.jsonl``: one row per case and
  configuration (first relevant rank, reciprocal rank, latency, violations).

Column names avoid ``@`` (``recall@5`` becomes ``recall_at_5``). Nothing is invented:
rows only restate what the result file contains, and the SYNTHETIC flag is carried
into every row so queries cannot silently mix it up with real data.
"""

import argparse
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_METRIC_NAME = re.compile(r"^(recall|precision|ndcg)@(\d+)$")
_RUN_FIELDS = (
    "cases",
    "cases_with_relevant_facts",
    "mrr",
    "temporal_correctness",
    "authorization_correctness",
    "forbidden_results",
    "latency_ms_p50",
)


def column_name(metric: str) -> str:
    """``recall@5`` -> ``recall_at_5``; other names are returned unchanged."""
    match = _METRIC_NAME.match(metric)
    return f"{match.group(1)}_at_{match.group(2)}" if match else metric


def run_id(result: dict[str, Any]) -> str:
    """Stable id from the run timestamp and commit, e.g. ``20260925T005549-7c851db5``."""
    stamp = re.sub(r"[^0-9T]", "", str(result["date"]))[:15]
    return f"{stamp}-{str(result['commit_sha'])[:8]}"


def _common(result: dict[str, Any]) -> dict[str, Any]:
    dataset = result.get("dataset", {})
    return {
        "run_id": run_id(result),
        "run_timestamp": result["date"],
        "commit_sha": result["commit_sha"],
        "working_tree_dirty": bool(result.get("working_tree_dirty", False)),
        "dataset_name": dataset.get("name"),
        "dataset_version": dataset.get("version"),
        "synthetic": bool(dataset.get("synthetic", True)),
        "embedding_model": result.get("configuration", {}).get("embedding_model"),
    }


def run_rows(result: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """One row per configuration with its aggregate metrics."""
    common = _common(result)
    for configuration, metrics in sorted(result["metrics"].items()):
        row: dict[str, Any] = {**common, "configuration": configuration}
        for name, value in metrics.items():
            if name in _RUN_FIELDS or _METRIC_NAME.match(name):
                row[column_name(name)] = value
        yield row


def case_rows(result: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """One row per (configuration, case)."""
    common = _common(result)
    for configuration, cases in sorted(result.get("cases", {}).items()):
        for case in cases:
            relevant = set(case.get("relevant", []))
            ranks = [i for i, key in enumerate(case.get("ranked_keys", []), 1) if key in relevant]
            first = ranks[0] if ranks else None
            yield {
                **common,
                "configuration": configuration,
                "case_id": case["case_id"],
                "category": case.get("category"),
                "relevant_count": len(relevant),
                "returned_count": len(case.get("ranked_keys", [])),
                "first_relevant_rank": first,
                "reciprocal_rank": round(1 / first, 4) if first else 0.0,
                "latency_ms": case.get("latency_ms"),
                "vector_search": case.get("vector_search"),
                "violation_count": len(case.get("violations", [])),
            }


def export(results_dir: Path, out_dir: Path) -> list[Path]:
    """Write JSON Lines for every retrieval result in ``results_dir``; returns the files."""
    written: list[Path] = []
    for path in sorted(results_dir.glob("retrieval-*.json")):
        result = json.loads(path.read_text())
        if result.get("kind") != "retrieval-evaluation":
            continue
        run_date = str(result["date"])[:10]
        for table, rows in (("retrieval_runs", run_rows), ("retrieval_cases", case_rows)):
            target = out_dir / table / f"run_date={run_date}" / f"{run_id(result)}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps(row, sort_keys=True) for row in rows(result)]
            target.write_text("\n".join(lines) + "\n")
            written.append(target)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Export retrieval results for Athena")
    parser.add_argument("--results", type=Path, default=Path("../evaluation/results"))
    parser.add_argument("--out", type=Path, default=Path("../evaluation/analytics"))
    args = parser.parse_args()
    files = export(args.results, args.out)
    print(f"wrote {len(files)} files under {args.out}")  # noqa: T201


if __name__ == "__main__":
    main()
