# Observability (Phase 20)

Metrics with Prometheus, traces with OpenTelemetry, dashboards in Grafana,
and the JSON logs from Phase 1 tied to both. Design decision: ADR-034 in
[DECISIONS.md](DECISIONS.md).

```bash
make obs-up      # Prometheus http://localhost:9090, Grafana http://localhost:3001, Jaeger http://localhost:16686
make obs-down
```

Grafana's admin password is `GRAFANA_ADMIN_PASSWORD` (default
`change-me-local-only`; every port is bound to 127.0.0.1). The
"ContextLedger overview" dashboard is provisioned from
`infrastructure/observability/grafana/dashboards/`.

## Metrics: `GET /metrics`

Served by the API in Prometheus text format. Label values are bounded and
non-sensitive: HTTP routes are **templates**
(`/api/v1/organizations/{organization_id}/search`), unmatched paths share the
label `unmatched`, and no metric carries an organization, user, query, fact
value or prompt.

| Metric | Labels | Meaning |
|---|---|---|
| `contextledger_http_requests_total` | method, route, status | Requests |
| `contextledger_http_request_duration_seconds` | method, route | Latency histogram |
| `contextledger_retrievals_total` | vector_search, cache | Hybrid retrievals (vector used or fallback; cache hit/miss/off) |
| `contextledger_retrieval_duration_seconds` | | Retrieval latency, query embedding included |
| `contextledger_retrieval_results` | | Facts returned per retrieval |
| `contextledger_llm_calls_total` | provider, model, outcome | Model calls; outcome is `ok` or the error class |
| `contextledger_llm_tokens_total` | provider, model, direction | Input and output tokens |
| `contextledger_llm_call_duration_seconds` | provider, model | Model latency, retries included |
| `contextledger_answers_total` | status | Grounded answers: answered, insufficient_evidence, ungrounded |
| `contextledger_agent_runs_total` | status | Investigator runs by status |
| `contextledger_contradictions_detected_total` | kind | value_conflict (rule) or semantic (LLM suggestion) |
| `contextledger_revocations_total` | | Fact versions revoked |

Access: open locally. In staging and production the settings refuse to start
unless `CONTEXTLEDGER_METRICS_TOKEN` is set (or metrics are disabled with
`CONTEXTLEDGER_METRICS_ENABLED=false`); scrapers then send it as a bearer
token. `/metrics` is not part of the OpenAPI document.

## Traces: OpenTelemetry over OTLP/HTTP

Off unless `CONTEXTLEDGER_OTEL_EXPORTER_OTLP_ENDPOINT` is set:

- API in Compose: `http://jaeger:4318`
- `make run` on your Mac: `http://localhost:4318`

When on, spans cover every HTTP request (FastAPI instrumentation; health and
metrics excluded), every SQL statement (SQLAlchemy), outbound HTTP to OpenAI
(httpx), and explicit spans `retrieval.search`, `answers.answer`,
`llm.generate` (model, max tokens, token usage, attempts) and
`agent.investigate` (prompt version, status, steps, tool calls). Attributes
carry identifiers and counts, never fact values, prompts or answers.
`CONTEXTLEDGER_OTEL_SAMPLE_RATIO` (default 1.0) sets head sampling;
`CONTEXTLEDGER_OTEL_SERVICE_NAME` defaults to `contextledger-api`.

## Logs

Every JSON log line has `correlation_id` (the `X-Correlation-ID` echoed to
clients) and, when tracing is on, `trace_id` and `span_id` of the active span,
so a log line leads to its trace in Jaeger and back.

## Not covered yet

- The workers (embedding, graph projector, event relay and consumers) do not
  expose metrics; outbox size, projection lag and consumer lag need them
  (follow-up).
- No alerting rules; they depend on load-test baselines (Phase 27).
