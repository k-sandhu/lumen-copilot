# Observability — Arize Phoenix traces and Grafana metrics

Operator runbook for the `observability` compose profile
([ADR-0027](../architecture/0027-llm-observability-phoenix-and-metrics.md), #817).

| What | Where (local) | Fed by |
|---|---|---|
| Per-turn LLM traces | Arize Phoenix — <http://localhost:47188> | OTLP/HTTP traces from the backend and workers |
| Service and LLM metrics | Grafana — <http://localhost:47189>, dashboard **Lumen — service & LLM** | Prometheus (<http://localhost:47190>), which receives OTLP metrics |

## Turn it on

1. In `.env`, set `TELEMETRY_ENABLED=true`.
2. Start the profile, then recreate the app containers so they read the new
   setting:

   ```bash
   docker compose --profile observability up -d
   docker compose up -d --force-recreate backend worker media-worker
   ```

   If this checkout's `backend/pyproject.toml` gained the OpenTelemetry
   packages since your image was built, rebuild first:
   `docker compose build backend worker media-worker`.
3. Ask a question in the app. Its trace appears in Phoenix under the
   **lumen-copilot** project within a few seconds, and Grafana panels fill after
   the next 15-second metric export.

Turning it off is `TELEMETRY_ENABLED=false` plus a recreate. The profile's
containers can keep running or be stopped with
`docker compose --profile observability stop`.

## Reading a trace

Each answer turn is one trace rooted at `chat.turn` (AGENT). Underneath it:

- `llm.stream_tools` / `llm.chat` / `llm.stream` (LLM): the exact messages and
  tools the model received, its reply and tool calls, token counts (including
  cache read/write), and `lumen.llm.time_to_first_token_ms`.
- `tool.<name>` (TOOL): arguments, result, `lumen.tool.ok`/`error`. A governance
  refusal shows as an error with the refusal code.
- `retrieval.search` (RETRIEVER) with its `embedding` child: query, chunk ids,
  scores and passage text.

A headless/scheduled run is a `headless_run` (CHAIN) span carrying `lumen.run.id`,
with its turn nested inside. The root is OK on success and ERROR on an error
terminal, with only the typed error code (never the terminal's message).
Phoenix's **Sessions** tab groups turns by chat
session (`session.id`). Filter by user with `user.id`, or by tenant with
`lumen.tenant_id`.

## Content and access (read before any non-local deploy)

`TELEMETRY_CAPTURE_CONTENT` defaults to **true**: spans hold prompts, retrieved
tenant passages, completions and tool I/O. Phoenix shows every tenant's content
to whoever can open it, so:

- It is an **operator-only** tool. Never expose Phoenix to tenants or to the
  public internet.
- Outside `ENVIRONMENT=local`, run Phoenix with `PHOENIX_ENABLE_AUTH=true` and a
  `PHOENIX_SECRET` of 32+ characters, supplied by the deploy (for example a
  `docker-compose.override.yml` with an `environment:` block for `phoenix`). They
  are not interpolated in `docker-compose.yml` because Phoenix refuses to start
  when `PHOENIX_SECRET` is set but empty. Create a system API key in Phoenix and
  set it as `TELEMETRY_TRACES_API_KEY`. The backend refuses to start with
  content capture on and no key outside local.
- Set `TELEMETRY_CAPTURE_CONTENT=false` to keep only ids, counts, names, scores,
  status and timing.
- Retention: Phoenix keeps traces in its `phoenixdata` volume until deleted.
  Configure a retention policy in Phoenix (Settings → Data retention), or
  remove the volume, according to the deploy's data rules.
- Secrets never reach a span: provider keys, `Authorization` values and the
  sandbox runner token are not passed to the tracing helpers
  (`tests/test_observability.py` guards the provider key).

## Metrics

Every process pushes over OTLP every `TELEMETRY_METRICS_EXPORT_INTERVAL_SECONDS`
(15 s). Prometheus names them with unit suffixes:

| Instrument | Prometheus series | Labels |
|---|---|---|
| `http.server.request.duration` | `http_server_request_duration_seconds_*` | `http_request_method`, `http_route` (template), `http_response_status_code` |
| `gen_ai.client.operation.duration` | `gen_ai_client_operation_duration_seconds_*` | `gen_ai_operation_name`, `gen_ai_request_model`, `error_type` |
| `lumen.llm.time_to_first_token` | `lumen_llm_time_to_first_token_seconds_*` | `gen_ai_request_model` |
| `lumen.llm.tokens` | `lumen_llm_tokens_total` | `gen_ai_operation_name`, `gen_ai_request_model`, `gen_ai_token_type` |
| `lumen.tool.calls` / `lumen.tool.duration` | `lumen_tool_calls_total`, `lumen_tool_duration_seconds_*` | `lumen_tool_name`, `outcome` |
| `lumen.retrieval.duration` | `lumen_retrieval_duration_seconds_*` | `outcome` |
| `lumen.chat.turns` / `lumen.chat.turn.duration` | `lumen_chat_turns_total`, `lumen_chat_turn_duration_seconds_*` | `outcome`, `mode` |

Every series also carries `job` (service), `instance` (host:pid) and
`lumen_process` (`api` / `worker`). Tenant and user ids are never labels.
Tool labels and `tool.<name>` span names use the resolved registry or per-run
tool name; unresolvable names collapse to `unknown`. The span's `tool.name`
attribute retains the raw requested name for diagnosis.

The dashboard lives in `docker/grafana/dashboards/lumen-service-llm.json` and is
read-only in the UI. Change the JSON in the repo and restart Grafana.

## Troubleshooting

- **No traces.** Check `docker compose exec backend python -c "from app.core.config import get_settings as g; print(g().telemetry_enabled)"`.
  The backend logs `telemetry.configured` at startup when export is on.
- **`Failed to export` warnings in app logs.** The collector is down or
  unreachable. Requests are unaffected: the batch is dropped after a bounded
  retry. Start the profile or blank the endpoint.
- **Worker spans missing.** Workers configure export per pool child at
  `worker_process_init`. Recreate the worker after changing `.env`.
- **`telemetry.reconfigure_ignored`.** A second setup in the same process
  requested different settings. The warning lists names only; the first
  configuration remains active. Restart the process to apply the changes.
- **A long prompt looks cut off.** Attributes are truncated at
  `TELEMETRY_MAX_ATTRIBUTE_CHARS` (65 536) in the trace only.
