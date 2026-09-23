"""Hybrid temporal retrieval latency on a synthetic benchmark dataset.

What it does:

1. Creates a throwaway database ``contextledger_bench_retrieval`` and migrates it
   to head with the project's Alembic migrations (same schema, triggers, indexes).
2. Creates one organization, admin and source through the real services, then
   bulk-loads a SYNTHETIC benchmark dataset of entities / facts / fact versions
   (seeded ``random``; several versions per fact with real valid-time and
   transaction-time ranges). The full-text trigger indexes every version.
3. Embeds every version with the offline ``deterministic:hash-v1`` provider
   (vectors sent as pgvector ``sparsevec`` text, since they are sparse), then
   builds the HNSW index and runs ANALYZE.
4. Runs seeded queries through ``RetrievalService.search``: the same code path the
   API and MCP will use (permission checks, query embedding, one hybrid SQL
   statement, fusion, hydration). Mix: 70 % "now", 30 % historical ``valid_at``,
   20 % with ``known_at``.

Reported: end-to-end service latency p50 / p95 / p99 / mean (single client,
sequential, after warm-up), candidate counts, and a *sanity* check (is the
targeted fact in the top-k for "<property> of <entity>" questions?). The sanity
check is NOT a retrieval-quality evaluation: that is Phase 18.

Usage (repository root, `make up` running):
    make bench-retrieval                             # 10,000 fact versions
    make bench-retrieval BENCH_FACT_VERSIONS=100000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import subprocess
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.domain.embeddings import FACT_TEXT_TEMPLATE, document_hash, render_fact_document
from app.domain.facts import SourceType
from app.domain.tenancy import TenantContext
from app.providers.embeddings import DeterministicHashEmbeddingProvider
from app.services.facts import FactService
from app.services.organizations import OrganizationService
from app.services.retrieval import RetrievalQuery, RetrievalService
from app.services.tenancy import TenancyService
from app.services.users import UserService

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
BENCH_DB = "contextledger_bench_retrieval"
SEED = 42
T0 = datetime(2025, 1, 1, tzinfo=UTC)
HORIZON_HOURS = 24 * 365
PROVIDER = DeterministicHashEmbeddingProvider()

ENTITY_TYPES = ("customer", "vendor", "account", "order")
CITIES = ("Berlin", "Lisbon", "Austin", "Toronto", "Osaka", "Nairobi", "Lima", "Oslo", "Pune")
STATUSES = ("active", "suspended", "closed", "pending review", "on hold")
CURRENCIES = ("USD", "EUR", "GBP", "JPY", "INR", "CAD")
RATINGS = ("low", "medium", "high", "critical")
TERMS = ("net 15", "net 30", "net 45", "net 60", "prepaid")
PROPERTIES: dict[str, Any] = {
    "credit_limit": lambda r: r.choice((500, 1000, 2000, 5000, 7500, 10000, 25000)),
    "shipping_city": lambda r: r.choice(CITIES),
    "account_status": lambda r: r.choice(STATUSES),
    "preferred_currency": lambda r: r.choice(CURRENCIES),
    "risk_rating": lambda r: r.choice(RATINGS),
    "payment_terms": lambda r: r.choice(TERMS),
    "billing.contact_email": lambda r: f"billing{r.randrange(10_000)}@example.com",
    "support.tier": lambda r: r.choice(("basic", "standard", "premium", "enterprise")),
}
PRIVACY = ("PUBLIC", "INTERNAL", "INTERNAL", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")


@dataclass(frozen=True, slots=True)
class Version:
    id: uuid.UUID
    fact_id: uuid.UUID
    entity_type: str
    external_id: str
    property: str
    value: Any
    version: int
    valid_from: datetime
    valid_until: datetime | None
    supersedes_id: uuid.UUID | None


def _uuid(rng: random.Random) -> uuid.UUID:
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _upgrade(connection: Connection) -> None:
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logging"] = False
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def _recreate(url: URL, *, drop_only: bool = False) -> None:
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{BENCH_DB}" WITH (FORCE)'))
            if not drop_only:
                await conn.execute(text(f'CREATE DATABASE "{BENCH_DB}"'))
    finally:
        await admin.dispose()


def _generate(rng: random.Random, target_versions: int) -> list[Version]:
    versions: list[Version] = []
    entity_number = 0
    while len(versions) < target_versions:
        entity_number += 1
        entity_type = rng.choice(ENTITY_TYPES)
        external_id = f"{entity_type}-{entity_number:06d}"
        for prop in rng.sample(sorted(PROPERTIES), 4):
            fact_id = _uuid(rng)
            count = rng.choice((1, 1, 2, 2, 3))
            starts = sorted(rng.sample(range(HORIZON_HOURS), count))
            previous: uuid.UUID | None = None
            for n, start in enumerate(starts, start=1):
                version_id = _uuid(rng)
                end = starts[n] if n < count else None
                versions.append(
                    Version(
                        id=version_id,
                        fact_id=fact_id,
                        entity_type=entity_type,
                        external_id=external_id,
                        property=prop,
                        value=PROPERTIES[prop](rng),
                        version=n,
                        valid_from=T0 + timedelta(hours=start),
                        valid_until=None if end is None else T0 + timedelta(hours=end),
                        supersedes_id=previous,
                    )
                )
                previous = version_id
    return versions


async def _seed_tenant(engine: AsyncEngine) -> tuple[TenantContext, uuid.UUID]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        user = await UserService(session).register(
            email="bench-admin@example.com", display_name="Benchmark Admin"
        )
    async with sessions() as session:
        org = await OrganizationService(session).create(
            name="Benchmark Org", slug="benchmark-org", creator_user_id=user.id
        )
    async with sessions() as session:
        ctx = await TenancyService(session).resolve(organization_id=org.id, user_id=user.id)
    async with sessions() as session:
        source = await FactService(session).register_source(
            ctx,
            name="synthetic-billing",
            source_type=SourceType.SYSTEM_OF_RECORD,
            default_authority=90,
        )
    return ctx, source.id


async def _bulk_load(
    engine: AsyncEngine,
    rng: random.Random,
    ctx: TenantContext,
    source_id: uuid.UUID,
    versions: Sequence[Version],
) -> None:
    org = ctx.organization_id
    entity_ids: dict[str, uuid.UUID] = {}
    entity_rows, fact_rows, seen_facts = [], [], set()
    for v in versions:
        if v.external_id not in entity_ids:
            entity_ids[v.external_id] = _uuid(rng)
            entity_rows.append(
                {"id": entity_ids[v.external_id], "o": org, "t": v.entity_type, "x": v.external_id}
            )
        if v.fact_id not in seen_facts:
            seen_facts.add(v.fact_id)
            fact_rows.append(
                {"id": v.fact_id, "o": org, "e": entity_ids[v.external_id], "p": v.property}
            )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO entities (id, organization_id, entity_type, external_id) "
                "VALUES (:id, :o, :t, :x)"
            ),
            entity_rows,
        )
        await conn.execute(
            text(
                "INSERT INTO facts (id, organization_id, entity_id, property) "
                "VALUES (:id, :o, :e, :p)"
            ),
            fact_rows,
        )
    batch: list[dict[str, Any]] = []
    for v in versions:
        recorded = v.valid_from + timedelta(minutes=1)
        batch.append(
            {
                "id": v.id,
                "o": org,
                "f": v.fact_id,
                "n": v.version,
                "val": json.dumps(v.value),
                "s": source_id,
                "vf": v.valid_from,
                "vu": v.valid_until,
                "ob": v.valid_from,
                "ra": recorded,
                "vura": None if v.valid_until is None else v.valid_until + timedelta(minutes=1),
                "sup": v.supersedes_id,
                "a": rng.choice((60, 75, 90, 100)),
                "c": rng.choice(("0.700", "0.900", "1.000")),
                "ps": rng.choice(PRIVACY),
            }
        )
        if len(batch) == 2000:
            await _insert_versions(engine, batch)
            batch = []
    if batch:
        await _insert_versions(engine, batch)


async def _insert_versions(engine: AsyncEngine, rows: list[dict[str, Any]]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO fact_versions (id, organization_id, fact_id, version, value, "
                "source_id, valid_from, valid_until, observed_at, recorded_at, "
                "valid_until_recorded_at, supersedes_id, authority, confidence, privacy_scope) "
                "VALUES (:id, :o, :f, :n, CAST(:val AS jsonb), :s, :vf, :vu, :ob, :ra, :vura, "
                ":sup, :a, CAST(:c AS numeric), :ps)"
            ),
            rows,
        )


def _sparse(vector: Sequence[float]) -> str:
    entries = ",".join(f"{i + 1}:{x:.6f}" for i, x in enumerate(vector) if x != 0.0)
    return f"{{{entries}}}/{len(vector)}"


async def _embed(engine: AsyncEngine, org: uuid.UUID, versions: Sequence[Version]) -> float:
    started = time.perf_counter()
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX IF EXISTS ann_fact_embeddings_embedding_cosine"))
    for start in range(0, len(versions), PROVIDER.max_batch_size):
        chunk = versions[start : start + PROVIDER.max_batch_size]
        documents = [
            render_fact_document(v.entity_type, v.external_id, v.property, v.value) for v in chunk
        ]
        vectors = (await PROVIDER.embed(documents)).vectors
        rows = [
            {
                "o": org,
                "v": v.id,
                "m": PROVIDER.model_id,
                "t": FACT_TEXT_TEMPLATE,
                "h": document_hash(doc),
                "e": _sparse(vec),
            }
            for v, doc, vec in zip(chunk, documents, vectors, strict=True)
        ]
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO fact_embeddings (organization_id, fact_version_id, model, "
                    "dimensions, text_template, content_sha256, embedding) VALUES "
                    "(:o, :v, :m, 1536, :t, :h, CAST(CAST(:e AS sparsevec) AS vector(1536)))"
                ),
                rows,
            )
    async with engine.begin() as conn:
        await conn.execute(text("SET maintenance_work_mem = '512MB'"))
        # A parallel HNSW build shares its graph through /dev/shm, which Docker caps
        # at the container's shm_size. A serial build keeps it in local memory.
        await conn.execute(text("SET max_parallel_maintenance_workers = 0"))
        await conn.execute(
            text(
                "CREATE INDEX ann_fact_embeddings_embedding_cosine ON fact_embeddings "
                "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
            )
        )
        for table in (
            "entities",
            "facts",
            "fact_versions",
            "fact_embeddings",
            "fact_search_documents",
        ):
            await conn.execute(text(f"ANALYZE {table}"))
    return time.perf_counter() - started


@dataclass(frozen=True, slots=True)
class BenchQuery:
    request: RetrievalQuery
    target: uuid.UUID | None  # the version a "<property> of <entity>" question should find


def _queries(rng: random.Random, versions: Sequence[Version], count: int) -> list[BenchQuery]:
    latest = [v for v in versions if v.valid_until is None]
    queries = []
    for _ in range(count):
        v = rng.choice(latest)
        spoken = v.property.replace("_", " ").replace(".", " ")
        kind = rng.random()
        valid_at = None
        known_at = None
        if kind < 0.3:  # historical question
            valid_at = T0 + timedelta(hours=rng.randrange(HORIZON_HOURS))
            if rng.random() < 0.66:
                known_at = valid_at + timedelta(hours=rng.randrange(24 * 30))
        style = rng.random()
        if style < 0.6:
            question = f"What is the {spoken} of {v.external_id}?"
            target = v.id if valid_at is None else None
        elif style < 0.85:
            question = f"{v.entity_type}s with {spoken} {v.value}"
            target = None
        else:
            question = str(v.value)
            target = None
        queries.append(
            BenchQuery(RetrievalQuery(query=question, valid_at=valid_at, known_at=known_at), target)
        )
    return queries


def _percentile(ordered: list[float], pct: float) -> float:
    index = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
    return ordered[index]


async def run(fact_versions: int, query_count: int, warmup: int) -> dict[str, Any]:
    raw = os.environ.get("CONTEXTLEDGER_TEST_DATABASE_URL")
    if not raw:
        raise SystemExit("CONTEXTLEDGER_TEST_DATABASE_URL is not set; run via make")
    url = make_url(raw)
    await _recreate(url)
    engine = create_async_engine(url.set(database=BENCH_DB), pool_size=5)
    rng = random.Random(SEED)  # noqa: S311 (synthetic data, not security)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_upgrade)
        ctx, source_id = await _seed_tenant(engine)

        print(f"Generating {fact_versions} synthetic fact versions ...")
        versions = _generate(rng, fact_versions)[:fact_versions]
        # Truncating may cut a lineage chain after a closed version; that is still valid data.
        started = time.perf_counter()
        await _bulk_load(engine, rng, ctx, source_id, versions)
        load_seconds = time.perf_counter() - started
        print(f"  loaded in {load_seconds:.1f}s; embedding + HNSW build ...")
        embed_seconds = await _embed(engine, ctx.organization_id, versions)
        print(f"  embedded and indexed in {embed_seconds:.1f}s")

        queries = _queries(rng, versions, warmup + query_count)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        latencies: list[float] = []
        vector_candidates: list[int] = []
        text_candidates: list[int] = []
        with_results = 0
        targeted = found = 0
        for i, q in enumerate(queries):
            started = time.perf_counter()
            async with sessions() as session:
                result = await RetrievalService(session, PROVIDER).search(ctx, q.request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if i < warmup:
                continue
            latencies.append(elapsed_ms)
            vector_candidates.append(result.vector_candidates)
            text_candidates.append(result.text_candidates)
            with_results += bool(result.results)
            if q.target is not None:
                targeted += 1
                found += q.target in {r.version.id for r in result.results}

        async with engine.connect() as conn:
            server_version = await conn.scalar(text("SHOW server_version"))
            pgvector_version = await conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            counts = {
                table: await conn.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "entities",
                    "facts",
                    "fact_versions",
                    "fact_embeddings",
                    "fact_search_documents",
                )
            }
    finally:
        await engine.dispose()
        await _recreate(url, drop_only=True)

    ordered = sorted(latencies)
    return {
        "benchmark": "hybrid_retrieval_latency",
        "commit_sha": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": "synthetic benchmark dataset (seeded random entities, facts, versions)",
        "dataset_size": counts["fact_versions"],
        "environment": "local",
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "postgres": server_version,
            "pgvector": pgvector_version,
        },
        "configuration": {
            "row_counts": counts,
            "embedding_model": PROVIDER.model_id,
            "queries": len(latencies),
            "warmup_queries": warmup,
            "limit": 10,
            "query_mix": "70% now / 30% historical valid_at (2/3 of those with known_at)",
            "latency": "end-to-end RetrievalService.search, single client, sequential",
            "load_seconds": round(load_seconds, 1),
            "embed_and_index_seconds": round(embed_seconds, 1),
        },
        "results": {
            "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(_percentile(ordered, 95), 2),
            "p99_ms": round(_percentile(ordered, 99), 2),
            "mean_ms": round(statistics.fmean(ordered), 2),
            "max_ms": round(ordered[-1], 2),
            "mean_vector_candidates": round(statistics.fmean(vector_candidates), 1),
            "mean_text_candidates": round(statistics.fmean(text_candidates), 1),
            "queries_with_results": with_results,
            "sanity_target_in_top_10": {"targeted_queries": targeted, "found": found},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fact-versions", type=int, default=10_000)
    parser.add_argument("--queries", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    args = parser.parse_args()
    if args.fact_versions < 100 or args.queries < 1 or args.warmup < 0:
        raise SystemExit("--fact-versions >= 100, --queries >= 1, --warmup >= 0")

    result = asyncio.run(run(args.fact_versions, args.queries, args.warmup))
    r = result["results"]
    print(
        f"\n{result['dataset_size']} fact versions, {result['configuration']['queries']} queries: "
        f"p50={r['p50_ms']}ms p95={r['p95_ms']}ms p99={r['p99_ms']}ms mean={r['mean_ms']}ms"
    )
    sanity = r["sanity_target_in_top_10"]
    print(f"sanity: target in top-10 for {sanity['found']}/{sanity['targeted_queries']} questions")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sha = (result["commit_sha"] or "nocommit")[:8]
    stamp = result["date"][:19].replace(":", "").replace("-", "")
    out = RESULTS_DIR / f"retrieval-{stamp}-{result['dataset_size']}-{sha}.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
