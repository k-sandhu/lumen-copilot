# Async embedding client lifecycle (#780)

The supported Celery bridge creates a private event loop per `run_task` call.
Pinned LiteLLM 1.55.9 caches OpenAI embedding SDK clients by credentials, base,
timeout and retries, without the event loop. A later task can therefore reuse
an HTTP connection belonging to an already closed loop. Creating another
`LLMGateway` alone does not repair that vendor-global cache.

`app/llm/embedding_clients.py` owns the replacement transport lifecycle. The
gateway still calls `litellm.aembedding`, with unchanged model, request arguments,
float encoding, timeout, typed errors and response validation. When LiteLLM's
own provider resolver selects its **OpenAI adapter**, the gateway passes an
explicit async client, bypassing the vendor-global client cache. The registry is
keyed by owning loop and effective credentials/base/timeout/retries/organization.
Gateway instances on one loop reuse a client; a different loop gets a different
client. Registry access is locked across threads, and teardown removes only the
current loop's entries before awaiting their close hooks.

This covers the configured OpenRouter embedding endpoint (`openai/<author>/<model>`)
and native OpenAI through the same adapter, including no explicit `api_base`.
Effective native base, key and organization preserve pinned LiteLLM's existing
global/secret-manager environment fallback resolution, including `OPENAI_API_BASE`
and `OPENAI_ORGANIZATION`. The SDK retry count remains LiteLLM's existing
`DEFAULT_MAX_RETRIES`; no retry or model policy changes. Per-call credentials
and endpoints remain isolated. Credentials, vectors and cache keys are never
logged by the lifecycle owner.

Other provider adapters keep their generic LiteLLM path. An externally supplied
`litellm.aclient_session` also stays on that path: its caller owns that borrowed
HTTP transport. This change does **not** claim lifecycle support for every native
provider SDK, close borrowed clients or rewrite vendor-global caches. Revalidate
route resolution/client kwargs against the vendor adapter when upgrading LiteLLM.

Named teardown boundaries:

- `app/tasks/runner.py`: its `finally` retires this task loop's clients before
  database/Redis disposal and before `asyncio.run` closes the loop. Success,
  typed provider failure and cancellation all use this path.
- `app/main.py`: after answer producers drain, app lifespan shutdown retires
  only its serving loop's clients. Active clients on another loop are untouched.
- `tests/eval/test_chat_latency_live.py::_seed`: the fixture creates its own loop,
  so its `finally` retires embedding clients alongside its store and DB engine.
- Ad hoc async callers own their loop boundary: call
  `await app.llm.aclose_embedding_clients()` in `finally`, before closing it.
  `aclose_litellm_clients` is legacy isolated-test compatibility cleanup, including
  vendor-global clients; never use it as production concurrent-loop teardown.

Client close failures are best-effort and cannot mask the original task outcome.
Their registry entry is retired even on close failure, so it cannot be reused by
another loop. Cancellation is not converted to a provider error. Existing audit
and ingestion state transitions are unchanged.

Run the deterministic gateway/worker-boundary regressions from `backend/`:

```powershell
uv run --extra dev pytest tests/test_embedding_client_lifecycle.py tests/test_llm_gateway.py tests/test_tasks_event_loop.py -n 0 -o addopts= -q
```

The opt-in real regression calls the configured provider twice through actual
`run_task` loops, without clearing SDK caches or substituting a persistent loop:

```powershell
$env:RUN_LIVE = '1'
uv run --extra dev pytest tests/test_embedding_client_lifecycle.py -k live_two -n 0 -o addopts= -q -s
```

Use a disposable configured environment. The historical safe reproducer remains
`qa/reproductions/embedding_loop.py --live`. A healthy live baseline can pass if
the provider does not leave a reusable connection; that does not replace the
red/green deterministic closed-loop regression. The real regression reports only
success and dimensions, never vectors or raw provider errors.

The [HTTP/WebSocket latency harness](chat-latency-harness.md) supplements the
lifecycle proof with real grounded answers. Keep its five samples and original
2s p50/30s typed-error budgets; a timing failure is reported as a failure, not
relabeled with a relaxed budget. Worker ingestion additionally needs a real
Celery worker, object store, database and retrieval index; use synthetic documents,
check `ready`/chunk count and audit events, then delete and read back 404.
