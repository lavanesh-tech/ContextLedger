"""OpenTelemetry tracing (optional).

Enabled only when ``CONTEXTLEDGER_OTEL_EXPORTER_OTLP_ENDPOINT`` is set (for
example ``http://localhost:4318`` for the Jaeger container in
docker-compose's ``observability`` profile). Without it, the OpenTelemetry API
is a no-op and nothing is exported.

What is traced: every HTTP request (FastAPI), every SQL statement
(SQLAlchemy), outbound HTTP to the model/embedding provider (httpx), and
explicit spans for retrieval, model calls and agent runs. Span attributes
carry identifiers and counts, never fact values, prompts or answers.
"""

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.core.config import Settings

logger = logging.getLogger("contextledger.tracing")

tracer = trace.get_tracer("contextledger")


def configure_tracing(settings: "Settings", app: "FastAPI", engine: "AsyncEngine") -> bool:
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return False
    # Imported lazily: the SDK and instrumentations are only loaded when tracing is on.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    from app import __version__

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": __version__,
                "deployment.environment": settings.environment.value,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls="metrics,api/v1/health"
    )
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    logger.info("tracing.enabled", extra={"endpoint": endpoint})
    return True


def current_trace_ids() -> tuple[str | None, str | None]:
    """(trace id, span id) of the active span, for log correlation."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"
