"""Runs the grounded-answer evaluation and writes machine-readable results.

Two modes, reported separately:

* ``deterministic`` (default, free, used in CI via tests): a scripted model.
  It measures the PIPELINE only: were the right fact versions supplied, was
  anything forbidden supplied, is an invented citation always withheld. It says
  nothing about answer quality, and its results are labelled that way.
* ``live`` (opt-in, costs money): OpenAI, per prompt version. Measures answer
  accuracy, temporal correctness, insufficient-evidence handling, citation
  validity, structured-output validity, latency, tokens and (if you pass current
  prices) estimated cost. Requires ``--live`` AND an API key; ``--limit`` caps cases.

Every case runs in its own fresh organization in a throwaway database
(``contextledger_eval``), through the real services: retrieval, authorization,
the versioned prompt, the LangChain chain and the citation check.

    make eval                                    # deterministic
    make eval-live PROMPTS=grounded-answer-v1,grounded-answer-v2 LIMIT=18
"""

import argparse
import asyncio
import json
import os
import platform
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import httpx
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.grounding import AnswerStatus
from app.ai.providers import (
    FakeGenerationProvider,
    GenerationProvider,
    GenerationRequest,
    GenerationResult,
    OpenAIChatProvider,
)
from app.domain.facts import SourceType
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.evaluation.dataset import EvalCase, EvalDataset, load_dataset
from app.evaluation.metrics import CaseOutcome, answer_metrics, pipeline_metrics
from app.providers.embeddings import DeterministicHashEmbeddingProvider
from app.services.answers import AnswerGenerationError, AnswerQuery, GroundedAnswerService
from app.services.facts import FactService, RecordFactVersion
from app.services.memberships import MembershipService
from app.services.organizations import OrganizationService
from app.services.retrieval import RetrievalQuery, RetrievalResult, RetrievalService
from app.services.tenancy import TenancyService
from app.services.users import UserService

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DATASET: Final = REPO_ROOT / "evaluation" / "datasets" / "grounded_answers.json"
RESULTS_DIR: Final = REPO_ROOT / "evaluation" / "results"
ALEMBIC_INI: Final = REPO_ROOT / "backend" / "alembic.ini"
EVAL_DB: Final = "contextledger_eval"
EMBEDDINGS: Final = DeterministicHashEmbeddingProvider()
Sessions = async_sessionmaker[AsyncSession]


# --- models used by the deterministic mode ------------------------------------------------


def _labels(request: GenerationRequest) -> list[str]:
    content = request.messages[-1].content
    return [line[1 : line.index("]")] for line in content.splitlines() if line.startswith("[F")]


def echo_model() -> FakeGenerationProvider:
    """Cites every supplied fact. Exercises the pipeline; NOT a measure of answers."""

    def respond(request: GenerationRequest) -> str:
        labels = _labels(request)
        return json.dumps(
            {
                "answer": "Supplied facts: " + ", ".join(labels),
                "insufficient_evidence": not labels,
                "cited_facts": labels,
                "inferences": [],
            }
        )

    return FakeGenerationProvider(responder=respond, model="echo-citer")


def inventing_model() -> FakeGenerationProvider:
    """Always cites a label that was never supplied: the citation guard must withhold it."""

    def respond(request: GenerationRequest) -> str:
        return json.dumps(
            {
                "answer": "invented",
                "insufficient_evidence": False,
                "cited_facts": [*_labels(request), "F999"],
                "inferences": [],
            }
        )

    return FakeGenerationProvider(responder=respond, model="citation-inventor")


class RecordingProvider:
    """Wraps a provider and keeps what it was sent (to check what reached the model)."""

    def __init__(self, inner: GenerationProvider) -> None:
        self._inner = inner
        self.requests: list[GenerationRequest] = []
        self.last: GenerationResult | None = None

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        self.last = await self._inner.generate(request)
        return self.last


# --- one case ------------------------------------------------------------------------------


async def _organization(sessions: Sessions, label: str) -> TenantContext:
    stamp = UUID(bytes=os.urandom(16)).hex[:10]
    async with sessions() as session:
        admin = await UserService(session).register(
            email=f"eval-{label}-{stamp}@example.com", display_name=f"Eval {label}"
        )
    async with sessions() as session:
        org = await OrganizationService(session).create(
            name=f"Eval {label}", slug=f"eval-{label}-{stamp}", creator_user_id=admin.id
        )
    async with sessions() as session:
        return await TenancyService(session).resolve(organization_id=org.id, user_id=admin.id)


async def _caller(sessions: Sessions, admin: TenantContext, role: str) -> TenantContext:
    if role == "ADMIN":
        return admin
    stamp = UUID(bytes=os.urandom(16)).hex[:10]
    async with sessions() as session:
        user = await UserService(session).register(
            email=f"eval-caller-{stamp}@example.com", display_name="Eval caller"
        )
    async with sessions() as session:
        await MembershipService(session).add_member(
            admin, user_id=user.id, role=MembershipRole(role)
        )
    async with sessions() as session:
        return await TenancyService(session).resolve(
            organization_id=admin.organization_id, user_id=user.id
        )


async def _seed(sessions: Sessions, case: EvalCase) -> tuple[TenantContext, dict[str, UUID]]:
    orgs = {"self": await _organization(sessions, "self")}
    if any(f.tenant == "other" for f in case.facts):
        orgs["other"] = await _organization(sessions, "other")
    sources: dict[tuple[str, str], UUID] = {}
    versions: dict[str, UUID] = {}
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
        versions[spec.key] = version.id
    return await _caller(sessions, orgs["self"], case.caller_role), versions


async def evaluate_case(
    sessions: Sessions,
    case: EvalCase,
    model: GenerationProvider,
    *,
    prompt_version: str,
    max_output_tokens: int = 800,
    temperature: float | None = 0.0,
) -> CaseOutcome:
    caller, versions = await _seed(sessions, case)
    recorder = RecordingProvider(model)

    async def retrieve(ctx: TenantContext, query: RetrievalQuery) -> RetrievalResult:
        async with sessions() as session:
            return await RetrievalService(session, EMBEDDINGS).search(ctx, query)

    service = GroundedAnswerService(
        retrieve,
        recorder,
        prompt_version=prompt_version,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )
    expected_ids = {versions[key] for key in case.expect.cited_keys}
    started = time.perf_counter()
    try:
        result = await service.answer(
            caller, AnswerQuery(question=case.question, valid_at=case.valid_at)
        )
    except AnswerGenerationError as exc:
        return CaseOutcome(
            case_id=case.id,
            category=case.category,
            expected_status=case.expect.status,
            status="error",
            model_called=bool(recorder.requests),
            context_has_expected=False,
            context_leaks=_leaks(case, recorder),
            answer_matches=False,
            citations_cover_expected=False,
            rejected_citations=0,
            error=exc.cause,
            generation_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    answer = normalize_answer(result.answer or "")
    cited = {c.fact_version_id for c in result.citations}
    generation = result.generation
    return CaseOutcome(
        case_id=case.id,
        category=case.category,
        expected_status=case.expect.status,
        status=result.status.value,
        model_called=generation is not None,
        context_has_expected=expected_ids <= set(result.supplied_fact_version_ids),
        context_leaks=_leaks(case, recorder),
        answer_matches=(
            all(s.lower() in answer for s in case.expect.answer_contains)
            and not any(s.lower() in answer for s in case.expect.answer_excludes)
        ),
        citations_cover_expected=expected_ids <= cited,
        rejected_citations=len(result.rejected_citations),
        input_tokens=generation.input_tokens if generation else 0,
        output_tokens=generation.output_tokens if generation else 0,
        generation_ms=generation.latency_ms if generation else None,
        answer=result.answer,
    )


def normalize_answer(text: str) -> str:
    """Lower-case and drop thousands separators, so "$5,375" matches "5375"."""
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", text).lower()


def _leaks(case: EvalCase, recorder: RecordingProvider) -> list[str]:
    sent = "\n".join(m.content for r in recorder.requests for m in r.messages).lower()
    return [v for v in case.expect.forbidden_in_context if v.lower() in sent]


async def evaluate_dataset(
    sessions: Sessions,
    dataset: EvalDataset,
    model_factory: Callable[[], GenerationProvider],
    *,
    prompt_version: str,
    limit: int | None = None,
    max_output_tokens: int = 800,
    temperature: float | None = 0.0,
) -> list[CaseOutcome]:
    cases = dataset.cases[:limit] if limit else dataset.cases
    return [
        await evaluate_case(
            sessions,
            case,
            model_factory(),
            prompt_version=prompt_version,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        for case in cases
    ]


async def citation_guard(sessions: Sessions, dataset: EvalDataset, *, prompt_version: str) -> float:
    """Share of model calls whose invented citation was withheld (must be 1.0)."""
    outcomes = await evaluate_dataset(
        sessions, dataset, inventing_model, prompt_version=prompt_version
    )
    called = [o for o in outcomes if o.model_called]
    withheld = sum(o.status == AnswerStatus.UNGROUNDED.value for o in called)
    return withheld / len(called) if called else 1.0


# --- command line --------------------------------------------------------------------------


def _git(*args: str) -> str | None:
    # Fixed program and arguments from this module only (commit SHA for the record).
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _upgrade(connection: Connection) -> None:
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logging"] = False
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def _fresh_database(url: URL) -> URL:
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{EVAL_DB}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{EVAL_DB}"'))
    finally:
        await admin.dispose()
    eval_url = url.set(database=EVAL_DB)
    engine = create_async_engine(eval_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_upgrade)
    finally:
        await engine.dispose()
    return eval_url


def _record(
    *,
    dataset: EvalDataset,
    mode: str,
    model_id: str,
    prompt_version: str,
    outcomes: list[CaseOutcome],
    config: dict[str, Any],
    extra_metrics: dict[str, Any],
    prices: tuple[float | None, float | None],
) -> dict[str, Any]:
    metrics: dict[str, Any] = {"pipeline": pipeline_metrics(outcomes), **extra_metrics}
    if mode == "live":
        metrics["answers"] = answer_metrics(
            outcomes, price_input_per_million=prices[0], price_output_per_million=prices[1]
        )
    return {
        "kind": "grounded-answer-evaluation",
        "mode": mode,
        "date": datetime.now(UTC).isoformat(),
        "commit_sha": _git("rev-parse", "HEAD"),
        # Result files written by earlier runs do not make the code under test dirty.
        "working_tree_dirty": bool(
            _git("status", "--porcelain", "--", ".", ":!evaluation/results")
        ),
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "synthetic": dataset.synthetic,
            "cases": len(outcomes),
        },
        "model": model_id,
        "prompt_version": prompt_version,
        "configuration": config,
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "metrics": metrics,
        "cases": [asdict(o) for o in outcomes],
        "note": (
            "Deterministic mode uses a scripted model: pipeline metrics only; it does not "
            "measure answer quality."
            if mode == "deterministic"
            else "Live OpenAI run on a SYNTHETIC dataset; numbers describe this dataset only."
        ),
    }


async def run(args: argparse.Namespace) -> list[Path]:
    raw = os.environ.get("CONTEXTLEDGER_TEST_DATABASE_URL")
    if not raw:
        raise SystemExit("CONTEXTLEDGER_TEST_DATABASE_URL is not set; run via `make eval`")
    dataset = load_dataset(Path(args.dataset))
    prompts = [p.strip() for p in args.prompts.split(",") if p.strip()]
    engine = create_async_engine(await _fresh_database(make_url(raw)), pool_size=5)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    http_client: httpx.AsyncClient | None = None
    written: list[Path] = []
    try:
        if args.mode == "live":
            key = os.environ.get("CONTEXTLEDGER_OPENAI_API_KEY", "")
            if not key:
                raise SystemExit("live mode needs CONTEXTLEDGER_OPENAI_API_KEY")
            http_client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1/", timeout=args.timeout
            )
            client = http_client

            def factory() -> GenerationProvider:
                return OpenAIChatProvider(
                    client,
                    api_key=SecretStr(key),
                    model=args.model,
                    timeout_seconds=args.timeout,
                    max_retries=2,
                )
        else:
            factory = echo_model
        for prompt in prompts:
            count = args.limit or len(dataset.cases)
            print(f"[{args.mode}] {prompt}: evaluating {count} cases")  # noqa: T201
            outcomes = await evaluate_dataset(
                sessions,
                dataset,
                factory,
                prompt_version=prompt,
                limit=args.limit,
                max_output_tokens=args.max_output_tokens,
            )
            extra: dict[str, Any] = {}
            if args.mode == "deterministic":
                extra["citation_guard_withheld_rate"] = await citation_guard(
                    sessions, dataset, prompt_version=prompt
                )
            record = _record(
                dataset=dataset,
                mode=args.mode,
                model_id=factory().model_id,
                prompt_version=prompt,
                outcomes=outcomes,
                config={
                    "limit": args.limit,
                    "max_output_tokens": args.max_output_tokens,
                    "temperature": 0.0,
                    "embedding_model": EMBEDDINGS.model_id,
                    "facts_per_question": 8,
                },
                extra_metrics=extra,
                prices=(args.price_input, args.price_output),
            )
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = record["date"][:19].replace(":", "").replace("-", "")
            sha = (record["commit_sha"] or "nocommit")[:8]
            out = RESULTS_DIR / f"answers-{args.mode}-{prompt}-{stamp}-{sha}.json"
            out.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
            written.append(out)
            summary = record["metrics"].get("answers") or record["metrics"]["pipeline"]
            print(f"  wrote {out.relative_to(REPO_ROOT)}")  # noqa: T201
            print("  " + json.dumps(summary, default=str))  # noqa: T201
    finally:
        if http_client is not None:
            await http_client.aclose()
        await engine.dispose()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=["deterministic", "live"], default="deterministic")
    parser.add_argument("--live", action="store_true", help="confirm live (paid) OpenAI calls")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--prompts", default="grounded-answer-v1,grounded-answer-v2")
    parser.add_argument("--limit", type=int, default=None, help="evaluate only the first N cases")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--max-output-tokens", type=int, default=400)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--price-input", type=float, default=None, help="USD per 1M input tokens")
    parser.add_argument("--price-output", type=float, default=None, help="USD per 1M output tokens")
    args = parser.parse_args()
    if args.mode == "live" and not args.live:
        raise SystemExit("live mode calls OpenAI and costs money: add --live to confirm")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
