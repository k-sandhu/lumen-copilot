# 27. LLM observability and ops metrics — OpenTelemetry → Arize Phoenix + Prometheus/Grafana

- **Status:** Accepted (explicit sponsor instruction, 2026-10-29)
- **Date:** 2026-10-29
- **Tracking:** [#817](https://github.com/k-sandhu/lumen-copilot/issues/817) (F-AO-5 of epic [#300](https://github.com/k-sandhu/lumen-copilot/issues/300))
- **Extends:** [ADR-0003](0003-application-stack.md) §10 ("`structlog` + OpenTelemetry + Prometheus"), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (one owning module)

## Context

The backend already writes structured logs with request ids, the product audit
log, and per-answer rows (`llm_usage`, `tool_invocations`, `run_steps`). None of
them answer an operator's first questions about a bad turn: *which* model call
was slow, what the model actually saw, which tool failed, how often it happens.
That needs per-turn traces with the full LLM context, and service-level metrics.

ADR-0003 named OpenTelemetry + Prometheus but nothing was built. The sponsor
asked for **Arize** for LLM observability and for Grafana metrics, and decided
four things on 2026-10-29: self-hosted Phoenix (not the Arize AX cloud), prompt
and completion content captured by default, full turn trees, metrics included.

## Decision

### 1. One owning module, vendor-neutral wire

`backend/app/observability/` is the only module that imports the OpenTelemetry
SDK or exporters (AGENTS.md §6 row). Everything else calls its small helpers.
Traces and metrics leave the process over **OTLP/HTTP** (protobuf), a standard
protocol, so the sink is swappable by configuration. No Phoenix or Arize SDK is
a dependency. Added packages are all Apache-2.0: `opentelemetry-api`,
`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` and
`openinference-semantic-conventions` (attribute names only).

### 2. Traces → Arize Phoenix, self-hosted

Phoenix runs in an opt-in `observability` compose profile, beside the app. Trace
data, including tenant content, never leaves the stack.

**License exception to ADR-0003's OSS-only rule.** Phoenix is distributed under
the **Elastic License 2.0** (source-available, not OSI-approved). The sponsor
accepted this. It is scoped: ELv2 forbids offering Phoenix to third parties as a
managed service, and we run it only as an internal operator tool. No ELv2 code
is linked into the application, and the OTLP boundary keeps an OSI-licensed sink
(Jaeger, Tempo) a configuration change away.

Spans follow the **OpenInference** semantic conventions, which Phoenix renders
natively:

| Span | Kind | Where | Carries |
|---|---|---|---|
| `chat.turn` | AGENT | `ChatRuntime.run` (interactive, headless, preview) | `session.id`, `user.id`, tenant/assistant metadata, question in, answer out, finish reason, model used |
| `headless_run` | CHAIN | the headless-run Celery task | `lumen.run.id`, assistant, trigger; parent of its turn; OK on success, ERROR with the typed terminal error code on failure |
| `llm.<operation>` | LLM | `LLMGateway.chat` / `stream` / `stream_tools` | model, provider, invocation parameters, input messages, tools, output message, tool calls, prompt/completion/total and cache-read/cache-write token counts, time to first token |
| `embedding` | EMBEDDING | `LLMGateway.embed` | model, input count, input texts (no vectors), cache hit |
| `tool.<name>` | TOOL | `ToolRunner.run` | tool name, arguments, result, ok/error code, duration |
| `retrieval.search` | RETRIEVER | `RetrievalService.search` | query, document ids, names, scores, passage text |

LLM spans are written **by hand at the gateway chokepoint**, not by
monkey-patching LiteLLM. That means credentials (`api_key`, `api_base`,
cache-scope hints) can never enter `llm.invocation_parameters`, and token counts
come from the gateway's own `_extract_usage`, the same numbers `llm_usage`
stores. Streaming spans are not made "current" (an async generator can be
finalized in another context). They end exactly once, on completion, provider
error, or consumer close/cancel.
Malformed successful responses also end their spans before propagating the
original parsing error. Streams retain answer text for telemetry only while
their span records content; capture-off still records structure and usage.

No HTTP server spans are exported. A chat answer runs in a background task
after a 202, so a request span would be a short, misleading root, and
request-level timing is already covered by metrics (§3).

### 3. Metrics → Prometheus (OTLP receiver) → Grafana

Every process (API, each Celery worker child) **pushes** OTel metrics over OTLP
to Prometheus's native OTLP receiver (`--web.enable-otlp-receiver`). That avoids
a scrape endpoint per process and Prometheus's multiprocess mode for prefork
Celery. Instruments:

- `http.server.request.duration` (histogram, seconds), by method, route
  template and status code. Unmatched paths collapse to one label.
- `gen_ai.client.operation.duration` (histogram), by operation, model and
  error type.
- `lumen.llm.time_to_first_token` (histogram), by model.
- `lumen.llm.tokens` (counter), by operation, model and token type (input,
  output, cache_read, cache_write).
- `lumen.tool.calls` (counter) and `lumen.tool.duration` (histogram), by tool
  and outcome. Tool labels and `tool.<name>` span names use the resolved static
  registry or per-run tool name, otherwise `unknown`; only the `tool.name` span
  attribute keeps the raw requested name.
- `lumen.retrieval.duration` (histogram).
- `lumen.chat.turns` (counter) and `lumen.chat.turn.duration` (histogram), by
  outcome and interactive/headless mode.

Tenant and user ids are **never** metric labels (cardinality, and cross-tenant
exposure on a shared ops board). Grafana is provisioned from the repo with the
Prometheus datasource and a "Lumen — service & LLM" dashboard.

### 4. Content capture is on by default

`TELEMETRY_CAPTURE_CONTENT=true` is the default (sponsor decision). Spans then
hold prompts (including retrieved tenant passages), completions, tool arguments
and results, and retrieved passage text. Setting it to `false` drops every
content attribute, while ids, counts, names, scores, status and timing remain.
The rules that follow:

- **Secrets never enter a span**, whatever the capture setting. Provider keys,
  `Authorization` values and the sandbox runner token are not passed to the
  helpers. A test asserts that the configured key appears in no attribute.
- **Trace access is operator-only.** Phoenix is not a tenant-facing surface: it
  shows content across tenants. Outside `ENVIRONMENT=local`, Phoenix must run
  with authentication (`PHOENIX_ENABLE_AUTH`), and the exporter sends
  `TELEMETRY_TRACES_API_KEY` as a bearer token. The settings validator refuses a
  non-local deploy that exports content-bearing traces without a key.
- Attributes are bounded (`TELEMETRY_MAX_ATTRIBUTE_CHARS`, default 65 536
  characters, with a raised per-span attribute count). A pathological prompt is
  therefore truncated in the trace, never in the request.

### 5. Off by default, never on the request path

`TELEMETRY_ENABLED=false` is the default. The helpers then use a no-op tracer
and meter: no exporter is built and no socket is opened (offline tests are
unchanged). When enabled, export is batched on background threads. A down or
slow collector drops data, after bounded retries, and never fails or delays a
request. The API configures telemetry in its lifespan. Celery configures it per
child process (`worker_process_init`), because exporter threads do not survive
`fork`. Both flush on shutdown.

## Consequences

- Operators get a per-turn waterfall (retrieval → model calls → tools →
  synthesis) with the model's exact input and output, and Phoenix's Sessions
  view groups the turns of a conversation. Grafana shows request rates,
  latency, LLM error rates, TTFT and token burn across API and workers.
- The trace store holds tenant content and becomes a data store to govern: its
  access control and retention are now in scope for any non-local deploy
  (runbook: `docs/runbooks/observability.md`).
- Arize AX (cloud) is reachable later by pointing the OTLP endpoint and headers
  at it. That would send tenant content off-stack, so it needs its own sponsor
  decision.
- Not covered: an in-app, tenant-facing trace viewer (F-AO-2), usage analytics
  (F-AO-1), budgets (F-AO-3), alerting rules, browser tracing, and LLM-judge
  evaluations in Phoenix.
