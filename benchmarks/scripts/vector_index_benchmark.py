"""Vector index benchmark: exact scan vs IVFFlat vs HNSW on pgvector.

Data: a SYNTHETIC benchmark dataset of clustered vectors, generated inside
PostgreSQL with a fixed seed (``setseed``), in a separate database
``contextledger_bench`` that is dropped and recreated on every run. No real
user or customer data is involved.

For each strategy it measures:
* index build time and on-disk index size;
* client-side query latency p50 / p95 (Python -> PostgreSQL round trip);
* recall@10 against exact (brute-force) results.

Usage (from the repository root, with `make up` running):
    make bench-vector                 # 20,000 x 1536
    make bench-vector BENCH_ROWS=5000 # quicker run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
BENCH_DB = "contextledger_bench"
K = 10


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def _recreate_database(url: URL) -> None:
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{BENCH_DB}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{BENCH_DB}"'))
    finally:
        await admin.dispose()


async def _generate(
    conn: AsyncConnection, *, rows: int, dims: int, clusters: int, queries: int
) -> float:
    started = time.perf_counter()
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await conn.execute(text("SELECT setseed(0.42)"))
    await conn.execute(
        text(
            f"""
            CREATE TABLE centroids AS
            SELECT c AS id, ARRAY(SELECT random() * 2 - 1 + 0 * c FROM generate_series(1, {dims})) AS v
            FROM generate_series(1, {clusters}) AS c
            """
        )
    )
    await conn.execute(
        text(f"CREATE TABLE items (id int PRIMARY KEY, embedding vector({dims}) NOT NULL)")
    )
    await conn.execute(
        text(
            f"""
            INSERT INTO items
            SELECT i,
                   (SELECT array_agg(ce.v[j] + (random() - 0.5) ORDER BY j)
                    FROM generate_series(1, {dims}) AS j)::vector({dims})
            FROM generate_series(1, {rows}) AS i
            JOIN centroids ce ON ce.id = 1 + (i % {clusters})
            """
        )
    )
    await conn.execute(
        text(
            f"""
            CREATE TABLE queries AS
            SELECT q AS id,
                   (SELECT array_agg(x + (random() - 0.5) * 0.2 ORDER BY ord)
                    FROM unnest(it.embedding::real[]) WITH ORDINALITY AS t(x, ord))::vector({dims})
                   AS embedding
            FROM generate_series(1, {queries}) AS q
            JOIN items it ON it.id = 1 + ((q * 7919) % {rows})
            """
        )
    )
    await conn.execute(text("ANALYZE items"))
    return time.perf_counter() - started


async def _query_vectors(conn: AsyncConnection) -> list[str]:
    result = await conn.execute(text("SELECT embedding::text FROM queries ORDER BY id"))
    return [row[0] for row in result]


async def _run_queries(
    conn: AsyncConnection, vectors: list[str]
) -> tuple[list[list[int]], list[float]]:
    statement = text(f"SELECT id FROM items ORDER BY embedding <=> CAST(:q AS vector) LIMIT {K}")
    await conn.execute(statement, {"q": vectors[0]})  # warm-up
    results: list[list[int]] = []
    latencies: list[float] = []
    for vector in vectors:
        started = time.perf_counter()
        rows = await conn.execute(statement, {"q": vector})
        ids = [row[0] for row in rows]
        latencies.append((time.perf_counter() - started) * 1000)
        results.append(ids)
    return results, latencies


def _recall(found: list[list[int]], truth: list[list[int]]) -> float:
    hits = sum(len(set(f) & set(t)) for f, t in zip(found, truth, strict=True))
    return hits / (K * len(truth))


def _latency_stats(latencies: list[float]) -> dict[str, float]:
    ordered = sorted(latencies)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
        "mean_ms": round(statistics.fmean(ordered), 3),
    }


async def _index_size_mb(conn: AsyncConnection, name: str) -> float:
    size = await conn.scalar(text("SELECT pg_relation_size(CAST(:n AS regclass))"), {"n": name})
    return round(int(size or 0) / 1_000_000, 2)


async def _measure_index(
    conn: AsyncConnection,
    *,
    label: str,
    create_sql: str,
    index_name: str,
    settings: list[tuple[str, str]],
    vectors: list[str],
    truth: list[list[int]],
) -> list[dict[str, Any]]:
    started = time.perf_counter()
    await conn.execute(text(create_sql))
    build_seconds = round(time.perf_counter() - started, 3)
    size_mb = await _index_size_mb(conn, index_name)
    measurements = []
    for setting, value in settings:
        await conn.execute(text(f"SET {setting} = {value}"))
        found, latencies = await _run_queries(conn, vectors)
        measurements.append(
            {
                "strategy": label,
                "query_setting": f"{setting}={value}",
                "build_seconds": build_seconds,
                "index_size_mb": size_mb,
                "recall_at_10": round(_recall(found, truth), 4),
                **_latency_stats(latencies),
            }
        )
        print(
            f"  {label:8} {setting}={value:<4} recall@10={measurements[-1]['recall_at_10']:.3f} "
            f"p50={measurements[-1]['p50_ms']}ms p95={measurements[-1]['p95_ms']}ms"
        )
    await conn.execute(text(f"DROP INDEX {index_name}"))
    return measurements


async def run(rows: int, dims: int, clusters: int, queries: int) -> dict[str, Any]:
    raw = os.environ.get("CONTEXTLEDGER_TEST_DATABASE_URL")
    if not raw:
        raise SystemExit("CONTEXTLEDGER_TEST_DATABASE_URL is not set; run via `make bench-vector`")
    url = make_url(raw)
    await _recreate_database(url)
    engine = create_async_engine(url.set(database=BENCH_DB), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET maintenance_work_mem = '512MB'"))
            await conn.execute(
                text("SET max_parallel_maintenance_workers = 0")
            )  # comparable builds
            print(f"Generating synthetic dataset: {rows} x {dims} ({clusters} clusters) ...")
            generation_seconds = await _generate(
                conn, rows=rows, dims=dims, clusters=clusters, queries=queries
            )
            await conn.commit()
            vectors = await _query_vectors(conn)

            print("Exact search (sequential scan) ...")
            truth, exact_latencies = await _run_queries(conn, vectors)
            results: list[dict[str, Any]] = [
                {
                    "strategy": "exact",
                    "query_setting": "seq scan",
                    "build_seconds": 0.0,
                    "index_size_mb": 0.0,
                    "recall_at_10": 1.0,
                    **_latency_stats(exact_latencies),
                }
            ]
            lists = max(1, round(rows**0.5))
            results += await _measure_index(
                conn,
                label="ivfflat",
                create_sql=f"CREATE INDEX bench_ivfflat ON items USING ivfflat (embedding vector_cosine_ops) WITH (lists = {lists})",
                index_name="bench_ivfflat",
                settings=[
                    ("ivfflat.probes", "1"),
                    ("ivfflat.probes", "10"),
                    ("ivfflat.probes", "20"),
                ],
                vectors=vectors,
                truth=truth,
            )
            results += await _measure_index(
                conn,
                label="hnsw",
                create_sql="CREATE INDEX bench_hnsw ON items USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)",
                index_name="bench_hnsw",
                settings=[("hnsw.ef_search", "40"), ("hnsw.ef_search", "100")],
                vectors=vectors,
                truth=truth,
            )
            server_version = await conn.scalar(text("SHOW server_version"))
            pgvector_version = await conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
    finally:
        await engine.dispose()
        await _drop_bench_database(url)

    return {
        "benchmark": "vector_index_comparison",
        "commit_sha": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": "synthetic benchmark dataset (clustered random vectors, fixed seed)",
        "dataset_size": rows,
        "environment": "local",
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "postgres": server_version,
            "pgvector": pgvector_version,
        },
        "configuration": {
            "dimensions": dims,
            "clusters": clusters,
            "queries": queries,
            "k": K,
            "distance": "cosine",
            "ivfflat_lists": lists,
            "hnsw": {"m": 16, "ef_construction": 64},
            "latency": "client-side round trip, single connection, after one warm-up query",
            "generation_seconds": round(generation_seconds, 1),
        },
        "results": results,
    }


async def _drop_bench_database(url: URL) -> None:
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{BENCH_DB}" WITH (FORCE)'))
    finally:
        await admin.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--dims", type=int, default=1536)
    parser.add_argument("--clusters", type=int, default=64)
    parser.add_argument("--queries", type=int, default=200)
    args = parser.parse_args()
    for name in ("rows", "dims", "clusters", "queries"):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name} must be positive")

    result = asyncio.run(run(args.rows, args.dims, args.clusters, args.queries))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sha = (result["commit_sha"] or "nocommit")[:8]
    out = (
        RESULTS_DIR
        / f"vector-index-{result['date'][:19].replace(':', '').replace('-', '')}-{args.rows}-{sha}.json"
    )
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
