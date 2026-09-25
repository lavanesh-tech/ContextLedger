"""Retrieval evaluation (Phase 18): Recall@K, Precision@K, MRR, nDCG, and temporal /
authorization correctness of hybrid temporal retrieval.

    make eval-retrieval

For each case of ``evaluation/datasets/retrieval.json`` (SYNTHETIC): a fresh
organization is seeded (plus a second one for tenant-isolation cases), listed
versions are revoked, the embedding worker embeds everything with the offline
deterministic provider, and the query is run as the case's caller through
``RetrievalService`` (the same code as REST and MCP) under several
configurations:

* ``hybrid``           vector + full text, default trust weight
* ``hybrid_no_trust``  vector + full text, trust weight 0 (pure relevance)
* ``text_only``        embedding provider unavailable: full-text fallback

No LLM is involved and nothing costs money. The embedding provider is a
feature-hashing baseline, not a semantic model: paraphrase results measure
that baseline and would change with a real embedding model.
"""

import argparse
import asyncio
import json
import os
import platform
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.facts import SourceType
from app.domain.retrieval import DEFAULT_TRUST_WEIGHT
from app.domain.tenancy import TenantContext
from app.evaluation.dataset import T0, FactSpec
from app.evaluation.retrieval_metrics import RetrievalOutcome, outcome, summarize
from app.evaluation.runner import (
    EMBEDDINGS,
    REPO_ROOT,
    RESULTS_DIR,
    _caller,
    _fresh_database,
    _git,
    _organization,
)
from app.providers.embeddings import EmbeddingBatch, EmbeddingProvider, EmbeddingProviderError
from app.services.facts import FactService, RecordFactVersion
from app.services.retrieval import RetrievalQuery, RetrievalService
from app.services.revocations import RevocationService
from app.workers.embeddings import EmbeddingWorker

DATASET: Final = REPO_ROOT / "evaluation" / "datasets" / "retrieval.json"
LIMIT: Final = 10
Sessions = async_sessionmaker[AsyncSession]
Reason = Literal["not_valid_at_T", "superseded", "revoked", "other_tenant", "privacy"]


class RetrievalFactSpec(FactSpec):
    revoked: bool = False


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    category: str
    query: str
    facts: list[RetrievalFactSpec]
    relevant: list[str]
    forbidden: dict[str, Reason] = {}
    valid_at_hours: float | None = None
    caller_role: Literal["ADMIN", "ENGINEER", "VIEWER"] = "ADMIN"

    @property
    def valid_at(self) -> datetime | None:
        return None if self.valid_at_hours is None else T0 + timedelta(hours=self.valid_at_hours)

    @model_validator(mode="after")
    def _keys_are_consistent(self) -> "RetrievalCase":
        keys = [f.key for f in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.id}: duplicate fact keys")
        unknown = (set(self.relevant) | set(self.forbidden)) - set(keys)
        if unknown:
            raise ValueError(f"{self.id}: unknown keys {sorted(unknown)}")
        if set(self.relevant) & set(self.forbidden):
            raise ValueError(f"{self.id}: a fact cannot be both relevant and forbidden")
        return self


class RetrievalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    synthetic: bool
    description: str
    cases: list[RetrievalCase]


def load_retrieval_dataset(path: Path) -> RetrievalDataset:
    return RetrievalDataset.model_validate(json.loads(path.read_text(encoding="utf-8")))


class UnavailableEmbeddings:
    """An embedding provider that always fails: forces the full-text fallback."""

    max_batch_size = 1

    @property
    def model_id(self) -> str:
        return EMBEDDINGS.model_id

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        raise EmbeddingProviderError("disabled for the text-only configuration")


CONFIGURATIONS: Final[dict[str, tuple[EmbeddingProvider, float]]] = {
    "hybrid": (EMBEDDINGS, DEFAULT_TRUST_WEIGHT),
    "hybrid_no_trust": (EMBEDDINGS, 0.0),
    "text_only": (UnavailableEmbeddings(), DEFAULT_TRUST_WEIGHT),
}


async def seed(sessions: Sessions, case: RetrievalCase) -> tuple[TenantContext, dict[UUID, str]]:
    """Create the case's organizations and facts; return the caller and version id -> key."""
    orgs = {"self": await _organization(sessions, "self")}
    if any(f.tenant == "other" for f in case.facts):
        orgs["other"] = await _organization(sessions, "other")
    sources: dict[tuple[str, str], UUID] = {}
    keys: dict[UUID, str] = {}
    revoke: list[tuple[TenantContext, UUID, UUID]] = []
    for spec in sorted(case.facts, key=lambda f: (f.tenant, f.property_name, f.hours)):
        ctx = orgs[spec.tenant]
        if (spec.tenant, spec.source) not in sources:
            async with sessions() as session:
                source = await FactService(session).register_source(
                    ctx,
                    name=spec.source,
                    source_type=SourceType.SYSTEM_OF_RECORD,
                    uri=None,
                    default_authority=90,
                )
            sources[(spec.tenant, spec.source)] = source.id
        async with sessions() as session:
            version = await FactService(session).record_version(
                ctx,
                RecordFactVersion(
                    entity_type=spec.entity_type,
                    external_id=spec.external_id,
                    property=spec.property_name,
                    value=spec.value,
                    source_id=sources[(spec.tenant, spec.source)],
                    valid_from=spec.valid_from,
                    authority=spec.authority,
                    confidence=spec.confidence,
                    privacy_scope=spec.privacy_scope,
                ),
            )
        keys[version.id] = spec.key
        if spec.revoked:
            revoke.append((ctx, version.fact_id, version.id))
    for ctx, fact_id, version_id in revoke:
        async with sessions() as session:
            await RevocationService(session).revoke(
                ctx, fact_id, reason="evaluation: known to be wrong", version_id=version_id
            )
    for ctx in orgs.values():
        worker = EmbeddingWorker(sessions, EMBEDDINGS, organization_id=ctx.organization_id)
        while (await worker.run_once()).claimed:
            pass
    return await _caller(sessions, orgs["self"], case.caller_role), keys


async def evaluate_case(sessions: Sessions, case: RetrievalCase) -> dict[str, RetrievalOutcome]:
    caller, keys = await seed(sessions, case)
    results: dict[str, RetrievalOutcome] = {}
    for name, (provider, trust_weight) in CONFIGURATIONS.items():
        started = time.perf_counter()
        async with sessions() as session:
            result = await RetrievalService(session, provider).search(
                caller,
                RetrievalQuery(
                    query=case.query,
                    limit=LIMIT,
                    valid_at=case.valid_at,
                    trust_weight=trust_weight,
                ),
            )
        results[name] = outcome(
            case_id=case.id,
            category=case.category,
            ranked_keys=[keys.get(r.version.id, str(r.version.id)) for r in result.results],
            relevant=case.relevant,
            forbidden=dict(case.forbidden),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            vector_search=result.vector_search,
        )
    return results


async def evaluate(
    sessions: Sessions, dataset: RetrievalDataset
) -> dict[str, list[RetrievalOutcome]]:
    by_config: dict[str, list[RetrievalOutcome]] = {name: [] for name in CONFIGURATIONS}
    for case in dataset.cases:
        for name, result in (await evaluate_case(sessions, case)).items():
            by_config[name].append(result)
    return by_config


def record(
    dataset: RetrievalDataset, by_config: dict[str, list[RetrievalOutcome]]
) -> dict[str, Any]:
    return {
        "kind": "retrieval-evaluation",
        "date": datetime.now(UTC).isoformat(),
        "commit_sha": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(
            _git("status", "--porcelain", "--", ".", ":!evaluation/results")
        ),
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "synthetic": dataset.synthetic,
            "cases": len(dataset.cases),
        },
        "configuration": {
            "limit": LIMIT,
            "embedding_model": EMBEDDINGS.model_id,
            "configurations": {
                name: {
                    "embeddings": "unavailable" if name == "text_only" else "used",
                    "trust_weight": w,
                }
                for name, (_, w) in CONFIGURATIONS.items()
            },
        },
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "metrics": {name: summarize(outcomes) for name, outcomes in by_config.items()},
        "cases": {name: [asdict(o) for o in outcomes] for name, outcomes in by_config.items()},
        "note": (
            "SYNTHETIC dataset and an offline feature-hashing embedding model; numbers describe "
            "this dataset and baseline only."
        ),
    }


async def run(args: argparse.Namespace) -> Path:
    raw = os.environ.get("CONTEXTLEDGER_TEST_DATABASE_URL")
    if not raw:
        raise SystemExit(
            "CONTEXTLEDGER_TEST_DATABASE_URL is not set; run via `make eval-retrieval`"
        )
    dataset = load_retrieval_dataset(Path(args.dataset))
    engine = create_async_engine(await _fresh_database(make_url(raw)), pool_size=5)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        print(f"[retrieval] evaluating {len(dataset.cases)} cases")  # noqa: T201
        data = record(dataset, await evaluate(sessions, dataset))
    finally:
        await engine.dispose()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = data["date"][:19].replace(":", "").replace("-", "")
    sha = (data["commit_sha"] or "nocommit")[:8]
    out = RESULTS_DIR / f"retrieval-{stamp}-{sha}.json"
    out.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"  wrote {out.relative_to(REPO_ROOT)}")  # noqa: T201
    for name, metrics in data["metrics"].items():
        headline = {k: metrics[k] for k in ("mrr", "recall@3", "precision@1", "ndcg@3")}
        correctness = {k: metrics[k] for k in ("temporal_correctness", "authorization_correctness")}
        print(f"  {name}: {json.dumps(headline)} {json.dumps(correctness)}")  # noqa: T201
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", default=str(DATASET))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
