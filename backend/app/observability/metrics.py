"""Prometheus metrics.

Label rules: only bounded, non-sensitive values. Routes are the *template*
(``/api/v1/organizations/{organization_id}/search``), never the raw path, and
no metric carries an organization, user, query or fact value: tenant
identifiers would explode cardinality and leak who uses the system.
"""

import time
from typing import Final

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import API_V1_PREFIX

LATENCY_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

HTTP_REQUESTS = Counter(
    "contextledger_http_requests_total", "HTTP requests.", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "contextledger_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
RETRIEVALS = Counter(
    "contextledger_retrievals_total",
    "Hybrid retrievals by vector-search status and cache outcome.",
    ["vector_search", "cache"],
)
RETRIEVAL_LATENCY = Histogram(
    "contextledger_retrieval_duration_seconds",
    "Hybrid retrieval latency (including query embedding).",
    buckets=LATENCY_BUCKETS,
)
RETRIEVAL_RESULTS = Histogram(
    "contextledger_retrieval_results",
    "Facts returned per retrieval.",
    buckets=(0, 1, 2, 3, 5, 10, 20, 50),
)
LLM_CALLS = Counter(
    "contextledger_llm_calls_total",
    "Model calls by provider, model and outcome (ok or the error class).",
    ["provider", "model", "outcome"],
)
LLM_TOKENS = Counter(
    "contextledger_llm_tokens_total", "Model tokens.", ["provider", "model", "direction"]
)
LLM_LATENCY = Histogram(
    "contextledger_llm_call_duration_seconds",
    "Model call latency (all retries included).",
    ["provider", "model"],
    buckets=LATENCY_BUCKETS,
)
ANSWERS = Counter("contextledger_answers_total", "Grounded answers by status.", ["status"])
AGENT_RUNS = Counter(
    "contextledger_agent_runs_total", "Decision-investigator runs by status.", ["status"]
)
CONTRADICTIONS = Counter(
    "contextledger_contradictions_detected_total", "Contradictions recorded.", ["kind"]
)
REVOCATIONS = Counter("contextledger_revocations_total", "Fact versions revoked.")


def route_template(scope: Scope) -> str:
    """The matched route's path template, with the router prefix it was included under.

    FastAPI records the route relative to its router, so ``/api/v1`` is restored
    from the request path when the template lacks it.
    """
    template: str | None = getattr(scope.get("route"), "path", None)
    if not template:
        return "unmatched"
    path: str = scope["path"]
    if (
        not path.startswith(template)
        and path.startswith(API_V1_PREFIX + "/")
        and not template.startswith(API_V1_PREFIX)
    ):
        return API_V1_PREFIX + template
    return template


class MetricsMiddleware:
    """Counts and times every HTTP request by method, route template and status."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record_status)
        finally:
            template = route_template(scope)
            method = scope["method"]
            HTTP_REQUESTS.labels(method, template, str(status)).inc()
            HTTP_LATENCY.labels(method, template).observe(time.perf_counter() - started)


def metrics_endpoint(token: str | None):  # type: ignore[no-untyped-def]
    """GET /metrics. With a token configured, callers must send it as a bearer token."""

    async def metrics(request: Request) -> Response:
        if token:
            supplied = Headers(scope=request.scope).get("authorization", "")
            if supplied != f"Bearer {token}":
                return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return metrics
