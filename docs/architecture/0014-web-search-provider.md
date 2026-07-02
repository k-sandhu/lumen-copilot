# 14. Web-search provider & egress (the `web_search` agent tool)

- **Status:** Accepted *(sponsor-delegated; provider pick recorded 2026-07-02 — the sponsor delegated the OSS-vs-hosted call to this ADR under the ADR-0003 OSS-only default, and the seam stays swappable)*
- **Date:** 2026-07-02
- **Builds on:** [ADR-0003](0003-application-stack.md) (OSS-only preference; every model call via the `llm/` gateway), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (module boundaries; adapters expose domain types, never vendor types), [ADR-0005](0005-local-run-and-developer-workflow.md) (one-command local stack; no `:latest`), [ADR-0008](0008-conflict-free-parallel-delivery.md) (serialized seam → parallel build; auto-discovered registry), [ADR-0009](0009-connector-framework-and-web-source.md) (the `connectors/web/fetch.py` **SSRF chokepoint** + the per-tenant fetch rate limit), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1 tenant isolation, INV-2 owner-or-grant, INV-3 citations, INV-6 audit)
- **Scope:** SPIKE — this ADR is the **only** deliverable of [#205](https://github.com/k-sandhu/lumen-copilot/issues/205); no product code, no migrations. It records the provider + egress + governance decision and sketches the data model / tool contract as **guidance** for the dependent features ([#219](https://github.com/k-sandhu/lumen-copilot/issues/219), [#221](https://github.com/k-sandhu/lumen-copilot/issues/221)).

## Context

The agent-tool platform ([epic #198](https://github.com/k-sandhu/lumen-copilot/issues/198)) turns the hardcoded three-tool chat loop into a governed registry (CC-A, [#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) and ships **internet search** as the first new capability (story **E3-12** "web" knowledge mode; **E8-5** competitive research from the public web). Today the runtime's tools (`search_text` / `search_documents` / `get_document`, `services/chat_tools.py`) all read the tenant's **own** permissioned corpus through the `retrieval/` chokepoint. A `web_search` tool reaches **outside** that corpus to the open internet — a different trust boundary (`risk:security`, egress) that needs a recorded decision rather than a default, because the provider choice touches three things the mission cares about:

- **OSS-only posture** ([ADR-0003](0003-application-stack.md)) — a hosted search API is a proprietary, paid third-party dependency.
- **Data egress** — running a search sends the (model-derived) query to whoever answers it; a hosted API also means a third party sees every tenant's queries.
- **Cost** — a per-query API bills per call; a self-hosted metasearch does not.

The server already fetches **user-supplied** URLs safely: [ADR-0009 §3](0009-connector-framework-and-web-source.md) put the entire SSRF defense in **one** chokepoint (`connectors/web/fetch.py`) — https-only, resolve-and-reject loopback/private/link-local/CGNAT/metadata ranges on **every** redirect hop, size + time caps, content-type allowlist, descriptive User-Agent, TOCTOU-safe IP pinning — plus a per-tenant fetch **rate limit** at the sync-enqueue boundary (`tasks/rate_limit.py`). Web search's **result-page fetch leg** is exactly that problem, so it must reuse that chokepoint rather than open a second egress path.

The three provider options considered (from [#205](https://github.com/k-sandhu/lumen-copilot/issues/205)):

| Option | OSS / self-host | Per-query cost | Egress to 3rd party | Notes |
|---|---|---|---|---|
| **SearXNG** *(chosen)* — self-hosted metasearch | **Yes (AGPL-3.0, OSI)** — add a compose service | **None** | Only to the upstream engines SearXNG queries (self-controlled) | Aggregates public engines; JSON output; fits OSS-only; adds one service (~small) to the stack; relevance is "good enough", not turnkey-tuned. |
| Tavily / Brave / Bing Web Search API — hosted | No (hosted API + key) | Per call | Every query egresses to the vendor | Turnkey, strong relevance, zero infra; a paid dependency + a key in CC-C; conflicts with the OSS-only default. |
| Reuse `connectors/web` fetch only (no search index) | n/a | n/a | n/a | **Not a search engine** — only fetches a *known* URL. Insufficient for "search the web"; but its **fetch leg** is reused below to retrieve result pages. |

## Decision

### 1. Provider — **self-hosted SearXNG** (AGPL-3.0), behind a swappable seam

Back the `web_search` tool with **SearXNG** run as a **docker-compose service** in the local stack, with **JSON output enabled** (`search.formats: [json]`) so the adapter gets structured results, not scraped HTML. This is the OSS-aligned pick ([ADR-0003](0003-application-stack.md)): AGPL-3.0 (OSI), **no per-query API key**, and no tenant queries egress to a commercial search vendor — SearXNG queries public engines under our own control.

- **Config** via `core/config.py` (`pydantic-settings`): the SearXNG **endpoint** URL, request timeout, default result count, and the enabled engines/categories. **No API key** is needed for SearXNG (that is the point). Because the endpoint is an internal service address, calls from the adapter to SearXNG itself are a **trusted internal hop**, distinct from the **untrusted result-page fetch** below.
- **Swappable alternative (recorded, not chosen):** if the sponsor later prefers turnkey relevance over self-hosting, a **hosted API (Tavily / Brave / Bing)** can replace SearXNG **behind the same `search_web/` seam** — callers (the tool, the runtime) do not change, because the adapter already exposes only the domain `WebSearchResult` (below). A hosted provider's **API key would live in CC-C** (config/secrets) and its adoption is a **new decision** (a superseding ADR or an accepted amendment), not a silent swap — the egress-to-third-party trade-off must be recorded when it is made.

### 2. Module boundary — **new `backend/app/search_web/` adapter** (domain types only)

A **new small adapter module** owns the search-provider client. It is deliberately **separate** from both:

- `backend/app/search/` ([ADR-0010](0010-dedicated-text-search-engine.md)) — that owns the OpenSearch store for the tenant's **own** indexed corpus (BM25 + kNN, permission-filtered). Web search is a **different** external system (the open internet), not the retrieval store; folding it in would blur "our permissioned corpus" with "the public web".
- `backend/app/connectors/web/` — that is an **ingestion** connector (fetch → extract → index a user-added source into the corpus). Web search does **not** ingest; it returns transient results the model reads live. It **reuses** that connector's `fetch.py`/`extract.py` for the egress leg, but its own concern (query a search provider) is distinct.

`search_web/` responsibilities:

- Owns the SearXNG (or, later, hosted-API) HTTP client; issues the search query; maps the provider's JSON into the **domain type** and returns it. **No vendor/response types leak upward** ([ADR-0004](0004-architecture-boundaries-and-adapters.md)) — the runtime and the tool see only:

  ```python
  # backend/app/domain/... (pure; no vendor imports)
  @dataclass(frozen=True, slots=True)
  class WebSearchResult:
      title: str
      url: str
      snippet: str
      published_at: datetime | None = None   # when the provider reports it
  ```

  A search returns an ordered `tuple[WebSearchResult, ...]` (top-N by provider rank). Swapping SearXNG for a hosted API changes only the mapping **inside** `search_web/`; the domain type and every caller are unchanged.

- **Boundary-table row to add (AGENTS.md §6 / supersedes the [ADR-0004](0004-architecture-boundaries-and-adapters.md) table) — needs human approval, §6 below:**

  | External system / concern | The single owning module | Nobody else may… |
  |---|---|---|
  | **Web search** (internet search provider) | `backend/app/search_web/` (SearXNG; hosted API swappable behind the seam) | call a web-search provider or import its client |

### 3. Egress control — reuse the **one** SSRF chokepoint + the per-tenant rate limit (load-bearing)

There are two outbound legs, and the untrusted one is funneled through the existing chokepoint — **no new egress path is opened**:

1. **Query leg** (adapter → SearXNG): a call to our **own internal** service endpoint. Trusted internal hop; still time-bounded (config timeout) and counted for rate-limiting (below).
2. **Result-page fetch leg** (retrieve a result URL for a cite-worthy snippet): the URLs come **from the open internet** (search results), i.e. effectively **attacker-influenceable** — the exact SSRF threat [ADR-0009 §3](0009-connector-framework-and-web-source.md) exists for. Every such fetch **MUST** go through `connectors/web/fetch.py` (`fetch_url`): https-only, resolve-and-reject blocked ranges on **every redirect hop**, size + time caps, content-type allowlist, descriptive User-Agent, IP-pinned (TOCTOU-safe). Passage extraction reuses `connectors/web/extract.py`. **A web-search fetch that bypasses that chokepoint is a blocking defect** — the same bar as the connector fetch (ADR-0009 §3).

- **Per-tenant rate limit:** reuse `tasks/rate_limit.py` (the Redis fixed-window per-tenant limiter) so one tenant cannot make the platform fan out unbounded outbound requests (search calls **and** result-page fetches) — a DoS/amplification pivot. A search that would exceed the window is **throttled/deferred**, mirroring the connector's admission model (no new HTTP error code). The same fail-open-on-Redis-outage posture applies; the SSRF guard remains the authoritative fetch-time control.

### 4. Result → answer — snippets, optional page fetch, and **web citations distinct from document citations**

The tool returns the **top-N** results (`title` / `url` / `snippet`). **Optionally** (config/policy, and gated by the same rate limit), it **fetches + extracts** the top few result pages via the reused chokepoint so the model can ground on **passage-level** text rather than only the provider's snippet.

- **Web citations are represented distinctly from document citations**, preserving **INV-3** ("cite only what was retrieved/fetched"):
  - a **document** citation points to a permitted, retrieved passage in the tenant's corpus (`document_id` + chunk char-offsets — the existing `retrieval/` → citation path);
  - a **web** citation carries the **URL** + the **fetched snippet** (the title and, when a page was fetched, the extracted passage text). It is **not** a `document_id`; it never implies the content is in the corpus. The model may cite a web result **only** with text that was actually returned by the search or fetched through the chokepoint — a URL that was listed but never fetched cannot be cited as if its body were read (INV-3 stays: cite what was retrieved/fetched, nothing more).
- Sketch of the tool result the runtime turns into web citations (analogous to `ToolOutcome` in `services/chat_tools.py`, but a **web** shape — not `RetrievedPassage`, which is corpus-only):

  ```text
  web_search(query, k) →
    results: [ { title, url, snippet, published_at?, fetched_passage? } ]   # fetched_passage present only if the page was fetched+extracted through connectors/web/fetch.py
  ```

  The tool declares itself into the CC-A registry ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) with a JSON-schema (`query`, optional `k`), a **risk tier**, and read-back — exactly like the existing `TOOL_SPECS`. The concrete WS envelope field for a web citation (vs. a document citation) is frozen **contract-first** ([ADR-0006](0006-contract-first-parallel-implementation.md)) in the dependent feature [#219](https://github.com/k-sandhu/lumen-copilot/issues/219)/[#221](https://github.com/k-sandhu/lumen-copilot/issues/221); this ADR fixes only that the two citation kinds are **distinct** and both obey INV-3.

### 5. Governance (E3-12) — admin per-tenant enable + knowledge-scope gate + audit

The `web_search` tool is **off by default** and offered to the model **only** when **both** hold:

- the **tenant admin has enabled web mode** for the tenant (a per-tenant setting — the reversible, audited **T1** admin write shape already used for tenant settings, `TENANT_SETTINGS_UPDATED`, spec 0004 §2.5); **and**
- the answering assistant's **`knowledge_scope` includes `web`** (E3-12: the KW/assistant chose to let this assistant use the web). If the scope excludes `web`, the tool is not advertised even where the tenant enabled it.

Both gates are **allow-list** decisions surfaced through the CC-A tool registry ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) — the tool simply is not offered when either gate is closed (fail-closed; a tenant with web disabled **cannot** invoke it — a mandatory negative test, per epic #198 DoD and spec 0004 §9).

- **Audit each search (INV-6).** Every `web_search` invocation emits **one** audit event through the single audit sink (`services/audit.py`; mission filter #4), recording the actor, tenant, the query (hashed, consistent with `retrieval.query`), the result count, and the fetched URLs. This is the **`tool.invoked`** event **CC-A introduces**: it is **not yet** in the `domain/audit.py` `AuditAction` taxonomy (which today stops at the retrieval/answer/source/permission/tenant actions). CC-A ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) **adds** the tool-invocation action to that taxonomy — an **additive**, deny-by-default extension (the set only grows; no existing action is relaxed), exactly as ADR-0009 added the `source.*` actions. This ADR does **not** edit the taxonomy (it ships no code); it records the requirement that CC-A land the audited `tool.invoked` action before the web tool ships. **Disclosure:** the answer surfaces that the web was used (the tool appears in the trace + the answer carries web citations), so a reader can tell a web-grounded claim from a corpus-grounded one.

## Consequences

- **Upside:** internet search lands **OSS-only, key-less, and with no tenant queries egressing to a commercial vendor**; the untrusted result-page fetch reuses the **one** hardened SSRF chokepoint and the per-tenant rate limit (no second egress path, no re-litigated SSRF); web answers stay **cited and auditable** (INV-3/INV-6) with web citations visibly distinct from corpus citations; the provider stays **swappable** behind `search_web/` if the sponsor later wants turnkey relevance.
- **Cost / risk:**
  - **A new compose service (SearXNG)** raises the local-run floor a little ([ADR-0005](0005-local-run-and-developer-workflow.md)); it is far lighter than the OpenSearch JVM and needs no key. Pinned image, healthcheck, named config — no `:latest`.
  - **Relevance is metasearch-grade, not vendor-tuned.** If it proves insufficient for the target queries, the recorded escape hatch is the hosted-API swap behind the seam (a new, egress-recording decision — §1).
  - **Egress is real** even with SearXNG (queries reach upstream public engines, result pages are fetched from arbitrary hosts). The mitigation is the reused chokepoint (SSRF) + rate limit + audit — the risk is **contained at the existing seam**, not eliminated.
  - **Correctness bar:** the mandatory negative tests are the governance gate (web-disabled tenant / non-`web` scope ⇒ tool not offered / not invocable) and the egress guard (a result-page fetch that does not pass through `connectors/web/fetch.py` is a defect). These are proven in the dependent feature PRs, not here.
- **Delivery (ADR-0008):** serialized seam → parallel build. This ADR is the seam decision. It **unblocks** and its dependents flip `blocked-by`:
  - **[#219](https://github.com/k-sandhu/lumen-copilot/issues/219)** — the `web_search` agent tool (adapter + tool registration + result-page fetch + web citations, backend);
  - **[#221](https://github.com/k-sandhu/lumen-copilot/issues/221)** — the web knowledge-mode toggle + web-citation rendering (UI).

  Both also depend on **CC-A** ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207), the tool registry + the audited `tool.invoked` action). This ADR does not touch product code (SPIKE).

## Resolved decisions (sponsor-delegated, 2026-07-02)

1. **Provider:** **self-hosted SearXNG** (AGPL-3.0), docker-compose service, JSON output — OSS-only, no per-query key, no query egress to a commercial vendor.
2. **Hosted API:** recorded as a **swappable alternative** behind the `search_web/` seam (key in CC-C); adopting it is a **new, egress-recording decision**, not a silent swap.
3. **Module boundary:** new **`backend/app/search_web/`** adapter exposing domain `WebSearchResult` only; **new boundary-table row proposed** (needs human approval — §6, AGENTS.md §6 is human-owned).
4. **Egress:** result-page fetch reuses the **`connectors/web/fetch.py` SSRF chokepoint** ([ADR-0009 §3](0009-connector-framework-and-web-source.md)); per-tenant rate limit reuses `tasks/rate_limit.py`. A bypass is a blocking defect.
5. **Result / citations:** top-N snippets, optional top-page fetch+extract; **web citations distinct from document citations** (URL + fetched snippet), INV-3 preserved.
6. **Governance:** admin per-tenant enable of web mode **AND** assistant `knowledge_scope` includes `web` ⇒ tool offered; each search audited (`tool.invoked`, the action **CC-A adds** to the taxonomy); web use disclosed in the trace + citations.
