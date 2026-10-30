# 31. Provider-billed cost and per-answer spend accounting

- **Status:** Proposed
- **Date:** 2026-10-30
- **Tracking:** [#832](https://github.com/k-sandhu/lumen-copilot/issues/832)
- **Builds on:** [ADR-0004](0004-architecture-boundaries-and-adapters.md), [ADR-0016](0016-context-engine-and-cache-first-prompting.md) §2.6

## Decision

Record the provider's billed request cost in USD, never a local price-map
estimate. Extraction and provider usage-accounting flags belong exclusively to
`app/llm/`. Callers receive `TokenUsage.cost_usd: Decimal | None`.
OpenRouter requests include `usage: {include: true}` in `extra_body`, merged
with existing cache hints. Other providers' requests are unchanged.

Only numeric costs are accepted: missing, boolean, string, negative, non-finite,
greater than USD 1000, or nonzero below USD 1e-30 means unpriced. A reported
zero remains a measured zero. The pinned LiteLLM usage object preserves extra
fields as attributes; its synthesized streaming terminal can discard cost.
The gateway therefore also checks the wrapper's original usage chunks, never
its estimated `response_cost` metadata. Embedding responses expose batch usage
only on the first vector; process-cache hits carry no usage because no request
was dispatched.

Each route scope stores the priced sum as nullable `NUMERIC(48,30)` and an
unpriced-call count. NULL means no call in that scope was priced; mixed scopes
retain the priced subtotal and report the missing calls explicitly. Historical
rows remain NULL with zero unpriced calls because their call count is unknown.
Failover closes and resets both counters with the existing token scope. The
answer terminal totals every scope; suggestions keep their own row. Summaries
also record their completion cost, including refused summaries.

An attempt dispatched to the provider that fails or is abandoned may have been
billed. Count it once as unpriced, including cancellation and failure before
tokens arrive. Gateway dispatch evidence distinguishes this from a local
preflight refusal. A successful response without usage is also one unpriced
call. No prompt, answer, or tool content enters spend telemetry.

Completion SDK retries are disabled at the gateway. The runtime's existing
bounded retry/failover path accounts each failed attempt before issuing another
request, so a later priced success cannot hide potentially billed failures.
Dispatch evidence comes from the adapter's pre-API-call event after provider
resolution and parameter validation, or a returned response; entering the
library alone supplies no evidence. The event callback reads only the event
type and never logs or retains request contents.

## Wire and follow-up

`done.data.usage` gains optional `costUsd` (nonnegative decimal string or null)
and `unpricedCalls` (nonnegative integer). The string avoids JSON float loss;
older producers and consumers remain compatible. The existing usage UI remains
unchanged. Repository reads remain tenant scoped; no new usage endpoint is added.

[#833](https://github.com/k-sandhu/lumen-copilot/issues/833) adds the optional
per-answer tool-loop ceiling in [spec 0028](../specs/0028-cancellation-lifecycle.md).
It checks the accumulated billed subtotal across failover scopes **before each
provider attempt**, including retries. A measured cost can overshoot by one
request because it arrives after dispatch. One separately bounded tool-free
synthesis remains outside this phase ceiling, so its billed cost is additional.
A configured ceiling stops tool-loop dispatch after the first unpriced request:
unknown spend cannot establish remaining headroom. The priced subtotal and
unpriced-call count are both retained in content-free outcome telemetry; no
estimate is substituted for missing measurements. The disclosure uses the same
`cost` kind for measured exhaustion and unavailable pricing, with policy wording
that does not claim a measured total for an unpriced run. The ceiling is off by
default; production deployments should set a finite positive value. No tenant
budget, soft warning tier, or price catalog is introduced.

## Reversal and validation

Extraction is confined to the gateway and can change with provider shapes.
The migration drops the two columns and checks on downgrade, losing their
accounting history. Offline gateway, runtime, tenant-isolation, schema, and
migration regression tests cover these semantics; no paid request is required.
