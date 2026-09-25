"""HTTP load test of the running API on a synthetic benchmark dataset (Phase 27).

What it does:

1. Seeds a fresh tenant **through the public REST API** (development-header auth, so
   local only): one admin user, one organization, one source, ``--entities`` customers
   with three properties each, a second version for a third of them.
2. Waits for the embedding worker, then for each concurrency level in ``--levels``
   runs a closed-loop test: N workers each send the next request as soon as the
   previous one returns, for ``--duration`` seconds after a ``--warmup`` period that
   is not recorded. Request mix (seeded, weights in ``MIX``):

   * ``search``        POST /organizations/{id}/search   (hybrid temporal retrieval)
   * ``entity_facts``  GET  /organizations/{id}/entities/customer/{ext}/facts
   * ``fact_write``    POST /organizations/{id}/facts    (new version: invalidates caches)
   * ``health``        GET  /health

3. Records client-side latency per scenario (p50 / p95 / p99 / max), throughput,
   and status codes. 429 (rate limited) is counted separately from errors so a
   throttled run cannot pass as a fast one.

Results go to ``benchmarks/results/load-<timestamp>-<commit>.json``. Numbers describe
this laptop, the Docker Compose stack and this synthetic dataset only: load generator
and server share one machine, so they compete for CPU.

Usage (repository root):
    make load-test                     # restarts the API with rate limiting off, then restores it
    make load-test LOAD_LEVELS=1,4,16 LOAD_DURATION=20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import statistics
import subprocess
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "benchmarks" / "results"
MIX = {"search": 50, "entity_facts": 30, "fact_write": 10, "health": 10}
PROPERTIES = ("credit_limit", "account_status", "risk_score")
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..100); 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered) + 0.5 - 1e-9))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class Tenant:
    user_id: str
    base: str
    source_id: str
    external_ids: list[str]


@dataclass
class LevelStats:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: dict[str, Counter[int]] = field(default_factory=lambda: defaultdict(Counter))
    transport_errors: Counter[str] = field(default_factory=Counter)
    # First response body per (scenario, status >= 400): makes a failing run diagnosable.
    error_samples: dict[str, str] = field(default_factory=dict)

    def summary(self, seconds: float) -> dict[str, Any]:
        scenarios: dict[str, Any] = {}
        total = throttled = errors = 0
        for name in sorted(set(self.latencies) | set(self.statuses)):
            lat = self.latencies[name]
            codes = self.statuses[name]
            n = sum(codes.values())
            n_429 = codes.get(429, 0)
            n_err = sum(c for code, c in codes.items() if code >= 400 and code != 429)
            total, throttled, errors = total + n, throttled + n_429, errors + n_err
            scenarios[name] = {
                "requests": n,
                "ok": n - n_429 - n_err,
                "rate_limited_429": n_429,
                "errors": n_err,
                "status_codes": {str(k): v for k, v in sorted(codes.items())},
                "latency_ms": {
                    "p50": round(percentile(lat, 50), 2),
                    "p95": round(percentile(lat, 95), 2),
                    "p99": round(percentile(lat, 99), 2),
                    "max": round(max(lat), 2) if lat else 0.0,
                    "mean": round(statistics.fmean(lat), 2) if lat else 0.0,
                },
            }
        transport = sum(self.transport_errors.values())
        return {
            "duration_s": round(seconds, 2),
            "requests": total,
            "throughput_rps": round(total / seconds, 1) if seconds else 0.0,
            "error_rate": round((errors + transport) / total, 4) if total else 0.0,
            "rate_limited_429": throttled,
            "transport_errors": dict(self.transport_errors),
            "error_samples": self.error_samples,
            "scenarios": scenarios,
        }


async def _post(client: httpx.AsyncClient, path: str, user: str | None, body: Any) -> Any:
    headers = {"X-ContextLedger-User-Id": user} if user else {}
    response = await client.post(path, json=body, headers=headers)
    response.raise_for_status()
    return response.json()


async def seed(client: httpx.AsyncClient, entities: int, rng: random.Random) -> Tenant:
    tag = uuid.uuid4().hex[:8]
    user = await _post(
        client, "/users", None, {"email": f"load-{tag}@example.com", "display_name": "Load"}
    )
    uid = str(user["id"])
    org = await _post(client, "/organizations", uid, {"name": "Load", "slug": f"load-{tag}"})
    base = f"/organizations/{org['id']}"
    source = await _post(
        client,
        f"{base}/sources",
        uid,
        {"name": "billing-db", "source_type": "SYSTEM_OF_RECORD", "default_authority": 90},
    )
    ext_ids = [f"customer-{i:05d}" for i in range(entities)]
    sem = asyncio.Semaphore(16)

    async def put(ext: str, prop: str, value: Any, hours: int) -> None:
        async with sem:
            await _post(
                client,
                f"{base}/facts",
                uid,
                {
                    "entity_type": "customer",
                    "external_id": ext,
                    "property": prop,
                    "value": value,
                    "source_id": source["id"],
                    "valid_from": (T0 + timedelta(hours=hours)).isoformat(),
                },
            )

    await asyncio.gather(
        *(put(ext, prop, rng.randint(1000, 9000), 0) for ext in ext_ids for prop in PROPERTIES)
    )
    await asyncio.gather(
        *(put(ext, "credit_limit", rng.randint(1000, 9000), 5) for ext in ext_ids[::3])
    )
    return Tenant(uid, base, str(source["id"]), ext_ids)


def request_for(scenario: str, tenant: Tenant, rng: random.Random) -> tuple[str, str, Any]:
    ext = rng.choice(tenant.external_ids)
    if scenario == "search":
        prop = rng.choice(PROPERTIES).replace("_", " ")
        return "POST", f"{tenant.base}/search", {"query": f"What is the {prop} of {ext}?"}
    if scenario == "entity_facts":
        return "GET", f"{tenant.base}/entities/customer/{ext}/facts", None
    if scenario == "fact_write":
        body = {
            "entity_type": "customer",
            "external_id": ext,
            "property": "risk_score",
            "value": rng.randint(1, 100),
            "source_id": tenant.source_id,
            "valid_from": datetime.now(UTC).isoformat(),
        }
        return "POST", f"{tenant.base}/facts", body
    return "GET", "/health", None


async def run_level(
    client: httpx.AsyncClient,
    tenant: Tenant,
    concurrency: int,
    duration: float,
    warmup: float,
    seed_value: int,
) -> dict[str, Any]:
    stats = LevelStats()
    names, weights = zip(*MIX.items(), strict=True)
    start = time.perf_counter()
    record_from = start + warmup
    stop_at = record_from + duration
    headers = {"X-ContextLedger-User-Id": tenant.user_id}

    async def worker(index: int) -> None:
        rng = random.Random(seed_value * 1000 + index)  # noqa: S311 (synthetic load, not security)
        while time.perf_counter() < stop_at:
            scenario = rng.choices(names, weights)[0]
            method, path, body = request_for(scenario, tenant, rng)
            sent = time.perf_counter()
            try:
                response = await client.request(method, path, json=body, headers=headers)
                status: int | None = response.status_code
            except httpx.HTTPError as exc:
                status = None
                if sent >= record_from:
                    stats.transport_errors[type(exc).__name__] += 1
            elapsed_ms = (time.perf_counter() - sent) * 1000
            if sent >= record_from and status is not None:
                stats.latencies[scenario].append(elapsed_ms)
                stats.statuses[scenario][status] += 1
                key = f"{scenario} {status}"
                if status >= 400 and key not in stats.error_samples:
                    stats.error_samples[key] = response.text[:500]

    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    return {"concurrency": concurrency, **stats.summary(duration)}


async def main_async(args: argparse.Namespace) -> Path:
    rng = random.Random(args.seed)  # noqa: S311 (synthetic data, not security)
    limits = httpx.Limits(max_connections=max(args.levels) + 8)
    async with httpx.AsyncClient(
        base_url=args.url.rstrip("/") + "/api/v1", timeout=30.0, limits=limits
    ) as client:
        (await client.get("/health")).raise_for_status()
        print(f"[load] seeding {args.entities} entities via the API")
        seeded = time.perf_counter()
        tenant = await seed(client, args.entities, rng)
        seed_seconds = time.perf_counter() - seeded
        print(f"[load] seeded in {seed_seconds:.1f}s; waiting {args.settle}s")
        await asyncio.sleep(args.settle)
        levels = []
        for level in args.levels:
            print(f"[load] concurrency {level}: {args.duration}s")
            result = await run_level(
                client, tenant, level, args.duration, args.warmup, args.seed + level
            )
            levels.append(result)
            if result["error_samples"]:
                print(f"  errors: {result['error_samples']}")
            print(
                f"  {result['throughput_rps']} req/s, error rate {result['error_rate']}, "
                f"429s {result['rate_limited_429']}, "
                f"search p95 {result['scenarios'].get('search', {}).get('latency_ms', {})}"
            )

    sha = _git("rev-parse", "HEAD")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    result = {
        "benchmark": "http_load",
        "commit_sha": sha,
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": "synthetic benchmark dataset (seeded customers seeded through the REST API)",
        "dataset_size": {
            "entities": args.entities,
            "fact_versions_seeded": args.entities * len(PROPERTIES)
            + len(range(0, args.entities, 3)),
        },
        "environment": "local (Docker Compose API; load generator on the same machine)",
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "configuration": {
            "url": args.url,
            "mix_weights": MIX,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "levels": args.levels,
            "seed": args.seed,
            "model": "closed loop (each worker waits for its response)",
        },
        "seed_seconds": round(seed_seconds, 2),
        "metrics": {"levels": levels},
        "note": "Client-side latency on one laptop; not a production capacity figure.",
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"load-{stamp}-{(sha or 'nogit')[:8]}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"[load] wrote {out.relative_to(REPO_ROOT)}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--entities", type=int, default=300)
    parser.add_argument(
        "--levels", type=lambda s: [int(x) for x in s.split(",")], default=[1, 8, 32]
    )
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--settle", type=float, default=15.0, help="wait for embeddings")
    parser.add_argument("--seed", type=int, default=27)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
