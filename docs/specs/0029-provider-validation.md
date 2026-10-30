# Spec 0029 — Evidence-based LLM provider validation

Tracking: [#585](https://github.com/k-sandhu/lumen-copilot/issues/585) (epic
[#572](https://github.com/k-sandhu/lumen-copilot/issues/572)).

Provider validation is evidence-based and never leaks raw network errors. A
provider's status is derived from five independent checks —
`config_valid`, `reachable`, `catalog_discovered`, `auth_verified`,
`capabilities_verified` — each `passed`, `failed`, `unverified`, or
`not_applicable`, persisted in `llm_providers.validation_checks` with
`validated_at` (staleness) and a stable safe `last_error_code`.

Statuses (`LlmProviderStatus`): `pending` (no probe yet), `reachable`
(catalog discovered; authentication and capabilities unverified), `ready`
(all required checks passed), `error` (last probe failed).

Rules:

- Model discovery (`GET {base_url}/models` on create, retarget, key rotation,
  and refresh) proves at most reachability + catalog. A public model list
  never implies key validity, so discovery alone yields at most `reachable`.
  `auth_verified` is `unverified` when a key is stored, `not_applicable` for
  an anonymous provider.
- The only path to `ready` is the explicit admin **Test connection** action
  (`POST /admin/llm-providers/{id}/test-connection`): one minimal bounded
  chat request (`max_tokens: 1`) to an admin-selected discovered model. It is
  a real upstream request and the provider may bill for it; the UI states
  this before execution. Success verifies authentication + chat capability.
  An unknown model is `422 test_model_unknown` before any network I/O.
  The required capability for this registration check is chat; tool and embedding
  conformance remain #586/#597. A successful response must contain a usable chat
  choice, not merely a 2xx JSON body. This proves acceptance of the configured
  credentials for that request, not that a server requires authentication.
- Changing `base_url` or the API key re-runs discovery, so a changed provider
  drops back to `reachable`/`error` until re-tested.
- **Only `ready` providers are routable.** The single predicate is
  `is_routable` in `backend/app/services/provider_models.py`; it gates both
  the chat model picker and completion routing. To reverse the strict rule,
  change that one predicate.
  Routing rechecks this predicate at execution time and refuses a provider that
  has become unavailable since model selection; it never falls through to a
  deployment's default credentials.
- **Grandfathering (migration `0051_provider_validation`).** Providers that
  were `ready` before this spec stay `ready` and routable with
  `validation_grandfathered = true` (flagged in Admin with a Test connection
  action) until their next validation run, after which the strict rule
  applies. Existing setups do not silently disappear on deploy.

Error boundary: all provider HTTP lives in `backend/app/llm/provider_catalog.py`.
Every failure is a typed `ProviderProbeError(stage, code, retryable)`; codes
are `blocked_address`, `dns_failed`, `timeout`, `connection_failed`,
`unauthorized`, `forbidden`, `not_found`, `rate_limited`, `provider_error`,
`unexpected_status`, `invalid_response`, `response_too_large`, and
`validation_failed`. Response bodies are streamed with a 1 MiB byte limit;
probes request identity encoding and reject compressed bodies before decoding.
The HTTP exchange has a total deadline, without redirects or automatic retries. Persisted
`last_error` is the fixed safe message for the code. No API response, audit
event, log line, or UI surface ever carries a secret (or any fragment of
one — reads expose only `has_api_key` presence), exception text, a resolved
IP, a stack trace, or an external documentation link. Blocked base URLs
still return `422 base_url_blocked` but with a generic detail. SSRF rules
are unchanged: https-only + resolve-all/reject-any + IP-pin before any
network I/O, re-checked on every probe.

Audit (INV-6): `llm_provider.discovered` records status + model count +
safe error code; `llm_provider.connection_tested` records status + tested
model + safe error code. Create/update audits record key presence only.
Operator logs are structured and redacted (provider id, stage, code,
retryability, request correlation id).

Provider reads expose the last `validation` result (including the original
correlation id); migrated rows have no result until revalidated. Configuration
mutations and probes serialize on a tenant-scoped row lock so an older probe
cannot mark changed credentials Ready. A failed discovery invalidates its old
catalog for testing until discovery succeeds again. Vault storage for provider
keys uses a presence-only hint, including the vault's own audit event.

Out of scope: model capability conformance gating (#586/#597) beyond the
single chat probe; billing; browser autocomplete (#580).

The shared DNS preflight remains synchronous. The selected model's successful
test is registration evidence, not a conformance claim for every catalog entry.
Append-only historical audit events are retained; migration 0051 scrubs old
provider/vault read hints and stored diagnostics, not past audit history.
Internal vault mutations outside the provider admin service remain a separate
invalidation follow-up, [#820](https://github.com/k-sandhu/lumen-copilot/issues/820);
the vault has no generic HTTP router. Provider-admin credential changes always
re-run discovery as specified above.
