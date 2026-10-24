# 30. Adopt SaaS Backbone as the platform layer, with Rust in the backbone's data plane

- **Status:** Proposed *(direction and the owner decisions below approved in session 2026-10-09; verified against the backbone source on 2026-10-24, see §10. Acceptance needs the owner's explicit accept and approval of the proposed `AGENTS.md` diff. Merging this ADR while it is Proposed authorizes no implementation and waives no gate, including the licence cutover gate in §9.)*
- **Date:** 2026-10-09 *(proposed as 0028)* · 2026-10-24 *(verification recorded; renumbered to 0030 because #698 took 0028)*
- **Tracking:** [#716](https://github.com/k-sandhu/lumen-copilot/issues/716)
- **Supersedes, in part:** [ADR-0003](0003-application-stack.md) §§1, 4, 5, 6, 7 (runtime mechanisms only); [ADR-0004](0004-architecture-boundaries-and-adapters.md) boundary-table rows; [ADR-0005](0005-local-run-and-developer-workflow.md) compose topology; [ADR-0010](0010-dedicated-text-search-engine.md) §3 module ownership; [ADR-0015](0015-scheduling-and-headless-runs.md) §§1, 4; [ADR-0022](0022-group-access-model.md) storage of groups and grants. The precise scope is in §8.
- **Amends a proposal:** *Rust ingestion core* ([#662](https://github.com/k-sandhu/lumen-copilot/issues/662), PR [#699](https://github.com/k-sandhu/lumen-copilot/pull/699), not yet accepted; numbered 0026 on its branch, a number `main` now gives to the CI ADR) — its rules stand; its home moves (§5). Below it is called *the Rust-core proposal*.
- **Amends, on the migration branch only:** [ADR-0026](0026-continuous-integration.md) *Continuous integration* — see §8.
- **Precedent:** `lumen-accountant` ADR-0022 *Adopt SaaS Backbone as the platform layer* and branch `backbone-migration` (all 39 acceptance rows green, 2026-10-03).

## Context

Lumen Copilot's backend is about 72,700 lines of Python. Roughly 11,000 of them are platform plumbing that every multi-tenant product rebuilds:

- JWT and refresh-token authentication, and Argon2 hashing;
- the database session and tenant-GUC binding;
- settings, errors and the problem+json mapping;
- the secrets cipher;
- the Celery app, RedBeat and the sync/async task bridge;
- the Redis WebSocket backplane;
- the S3 adapter;
- the LiteLLM gateway;
- the audit sink and its durable denial transaction.

The rest is product: retrieval, ingestion, the chat and agent runtime, assistants, connectors, MCP, the sandbox and the domain.

**SaaS Backbone** (`sual-ai/saas-backbone`) is the owner's shared platform for exactly that plumbing:

- **Python control plane:** the `backbone.*` packages. These are `kernel` (composition, unit of work, RLS), `identity`, `tenancy`, `authz` (OpenFGA), `audit` (a per-tenant hash chain), `jobs` and `events` (a Postgres transactional queue and outbox), `realtime`, `files`, `search`, `llm`, `assistant` (AG-UI), `actions`, `approvals`, `secrets`, `consent`, `privacy`, `history`, `usage` and `observability`.
- **Rust data plane:** `bb-gateway` (ticketed WebSocket/SSE with replay), `bb-worker` (a native job queue) and `bb-extract` + `bb-py` (the `backbone_native` PyO3 module: sniff, extract, chunk, redact, hash, detect sensitive data).

A product composes itself in one root as `Backbone(modules=[…platform…, …product…])` and builds its image `FROM backbone-python`. `lumen-accountant` migrated onto it on 2–3 Oct 2026. That migration replaced auth, storage, queue, sockets, the LLM loop and authz, and it pushed fixes upstream that the accountant now uses:

- `ParentPolicy` restrictive parent permissions;
- `EmbeddingSpec` semantic indexing on `IndexSpec`;
- the assistant's `AnswerValidator` and `ProcessingContextResolver`;
- files encrypted at rest, with signed grants to the native worker.

Three facts make the decision pressing for Copilot.

1. **Two Rust stacks are forming.** Epic [#661](https://github.com/k-sandhu/lumen-copilot/issues/661) opened 2026-10-08 and already has eleven draft PRs (#701–#715). They build `rust/crates/lumen-docintel{,-py}`: Rust 1.99, PyO3 0.29.3 abi3-py312, MIT OR Apache-2.0, about 7.5k lines per format branch. The backbone already ships `bb-extract`/`backbone_native` with its own toolchain. Merging #661 as planned means two PyO3 extensions in one process, two toolchains, two cargo-deny policies and two extraction cores.
2. **The open performance problems are structural.** The July audits found FastAPI fast (liveness p50 2.6 ms) and OpenSearch idle (about 1.5% CPU). Several bottlenecks have since been fixed (#487, #492, #512, #514). Three remain:
   - **No admission control.** Answers run as `asyncio.create_task` inside the API process (`backend/app/api/v1/chat.py:1049`).
   - **Database sessions held across model latency.**
   - **No metrics or traces.**

   Moving runs onto queue workers with a function-scoped unit of work fixes the first two by construction; the backbone `observability` module addresses the third.
3. **Demo data only.** Copilot holds no customer tenants, so schemas can be replaced rather than migrated in place.

**Evidence basis.** The first draft (2026-10-09) was written without read access to `sual-ai/saas-backbone`, from the backbone build session, the accountant's ADR-0022 and migration plan, and accountant code that imports the real ports. On 2026-10-24 every backbone claim and upstream item was checked against the source at `sual-ai/saas-backbone@12b81e9`, the backbone's head since 2026-10-02. §10 records the findings and §6 gives each upstream item's status. Copilot facts come from `main@69a74c5`.

## Decision

### 1. Composition

Copilot's backend becomes a backbone product.

```python
# backend/app/main.py — BACKBONE_APP=app.main:bb
bb = Backbone(modules=[*PLATFORM_MODULES, *copilot_modules()])
app = create_app(bb)
```

- **One image, every role.** The product image is built `FROM` a pinned backbone runtime image (by digest). It runs the API (`backbone serve`), the business worker with outbox relay (`backbone worker --with-relay`), the scheduler (`backbone scheduler`) and migrations (`backbone db upgrade`). It moves Python from 3.12 to the backbone runtime's 3.13 (`deploy/docker/python.Dockerfile`).
- **Product code becomes modules.** Each product area is a `backbone.kernel.Module` contributing:
  - routers, models, and migrations on its own Alembic branch;
  - an OpenFGA fragment;
  - tasks, schedules and search indexes;
  - assistant agents, actions, context providers, and privacy and parent policies.
- **Peers talk through ports.** Modules call backbone ports and their peers' public services. They never import backbone models, services or adapters, or a peer's internals.
- **Layering still holds.** `api → services → domain`, with adapters hung off services, still holds *inside* each product module (ADR-0004). The "single owning module" for a platform concern is now a backbone module (§8 table).

### 2. Disposition of every backend area

The backbone replaces these, and they are deleted once their callers are ported:

| Copilot code | Backbone replacement |
|---|---|
| `auth/`, `services/auth_service.py`, `api/deps.current_user/current_tenant/require_roles` | `identity` (cookie/token auth, CSRF), `Principal`, `RequestContext`, `UserCtx`/`TenantCtx`, `authz.deps.require_dep` |
| `db/session.py`, `db/tenant_context.py`, `db/base.py`, `db/audit_transactions.py` | `kernel.db` `Base`/`TenantMixin`, function-scoped unit of work that commits before the response, the same `app.tenant_id` GUC, and a **non-owner** app role (`backbone db create-app-role`, `backbone doctor`) |
| `core/config.py` (platform fields), `core/errors.py`, `core/logging.py` | `BackboneSettings` + product `ModuleSettings(section=…)`, `kernel.errors` |
| `core/crypto.py`, `services/secrets_service.py` | `secrets` vault |
| `services/audit.py` sink and the durable denial path | `uow.audit` (same transaction, hash-chained) and `AuditLog.record` (own transaction, for denials) |
| `tasks/celery_app.py`, `tasks/runner.py`, `tasks/scheduler.py` (RedBeat), `tasks/rate_limit.py` | `jobs` (`JobQueue.enqueue(task, uow=, idempotency_key=)`, `@task`, `Schedule`), `events`/outbox, `cache` rate limits |
| `realtime/` (Redis backplane, `chat_ws.py`) | `realtime` (`RealtimePublisher`, tickets) and `bb-gateway` |
| `storage/` (`ObjectStore`) | `files.FileAccess` |
| `llm/gateway.py`, `llm/openrouter_stt.py` | `llm.LLMProvider` (metered), `audio` |
| `groups`/`group_members`/`grants` tables and the hand-mirrored permission predicate (SQL `_document_permitted` and `search/filters.py`) | `tenancy` groups and OpenFGA tuples in a product `copilot.fga` |

These stay in Copilot as product modules, ported to backbone ports:

- domain types (`domain/`);
- repositories for product tables;
- retrieval orchestration (passage semantics, attempt and fingerprint admission);
- ingestion orchestration and the media pipeline;
- citations and `citation_access`;
- assistants (ADR-0011), schedules and runs as product data;
- connectors (`gdrive`, `web`);
- the sandbox (the Python side; `sandbox_runner` stays a separate service) and web search;
- MCP server records and policy, on top of the backbone assistant's MCP client (§6, U6);
- the context engine (ADR-0016) as assistant hooks;
- prompts and evals;
- the `AuditAction` taxonomy, as data validated before `uow.audit`.

These capabilities Copilot has and the backbone lacks or may lack go **upstream** (§6). They are built in the backbone and consumed from there, never kept as a Copilot copy.

**No duplicate platform.** Once a package has ported a concern, Copilot may not keep its own implementation of that concern as a fallback, shim or "temporary" second path. A missing backbone capability blocks the dependent surface (it stays disabled), or it is built upstream. The gate in §9 enforces this.

### 3. Runtime topology

`docker compose up` remains the one-command local stack (ADR-0005).

**Services**

| Service | Role |
|---|---|
| `postgres` | backbone-pinned image; `pgvector` is no longer used by Copilot |
| `openfga-migrate`, `openfga` | Postgres datastore, preshared key |
| `migrate` | owner credentials: `db upgrade && create-app-role && check` |
| `api` | `backbone serve` |
| `worker` | `--with-relay` |
| `media-worker` | a separate jobs queue, concurrency 1, replacing the Celery `media-ingestion` queue |
| `scheduler` | backbone scheduler |
| `bb-worker` | native queue |
| `bb-gateway` | realtime transport |
| `valkey` | cache and rate limits only |
| S3 store + bucket init | the backbone reference uses SeaweedFS; MinIO stays only if the backbone S3 adapter is verified against it in package F |
| `opensearch` | retrieval |
| `searxng` | web search |
| `sandbox-runner` + exec image | code execution |
| `frontend` | UI |

**Removed:** `redis` as broker and backplane, the Celery `worker`, `media-worker` and `beat`, the MinIO multipart reaper (the backbone tracks multipart sessions and cleans them up), and the `alembic upgrade head` entrypoint.

**Runtime rules**

- **Pin every image.** Every image is pinned by tag and digest.
- **Least-privilege database role.** The API, workers and gateway connect as the non-owner app role. FORCE RLS stays.
- **Isolated stack.** The compose project name, ports (`471xx`, ADR-0005) and volumes stay distinct from any other product stack on the host.

### 4. Search stays OpenSearch, as a backbone engine

OpenSearch remains the single retrieval store (ADR-0010 §§1–2, 4–5 stand). Its client moves out of Copilot and becomes a third engine behind backbone `search` (`BACKBONE_SEARCH__ENGINE=opensearch`), alongside Postgres FTS and Meilisearch.

**What the engine must implement** (upstream item U1):

- hybrid BM25 + kNN through a normalization pipeline;
- `IndexSpec` ACL fields (`acl_from`, `acl_parents`) and `post_check`;
- `EmbeddingSpec` vectors at the deployment width (2,048);
- publication semantics that are attempt-scoped and fingerprinted, with refresh-acknowledged readiness (ADR-0010 §5, #346);
- delete-by-generation;
- mirrored connector ACLs (`acl_principals` with a freshness window, ADR-0019).

**Permissions.** Copilot's `retrieval/` module stays the product's retrieval orchestrator and the only caller of `Searcher` for document passages. The permission predicate is no longer hand-written twice (SQL and engine filter). It derives from the OpenFGA model, plus index ACLs, plus a post-check. Hydration re-checks against OpenFGA before a passage can become a citation.

**Cutover rules**

- The cutover keeps ADR-0010's parity negatives.
- Package R must show retrieval quality and latency no worse than `main` on the existing evaluation set before Copilot's `search/` module is deleted.
- If U1 cannot meet ADR-0010 §4, package R stops. Copilot does **not** fall back to a local OpenSearch adapter (§2).

### 5. Rust lives in the backbone's data plane

1. **One native extension per process: `backbone_native`.** Copilot ships no PyO3 module and no Cargo workspace.
2. **Epic #661 relocates; it is not cancelled.**
   - **Crates move.** The `lumen-docintel` and `lumen-docintel-py` crates and their work in #701–#715 move into the backbone's Rust workspace. The core becomes a backbone crate (for example `bb-docintel`) beside `bb-extract`. Its bindings join `bb-py`, so it is exposed through `backbone_native` and runnable as `bb-worker` native jobs.
   - **One toolchain.** Toolchain, PyO3 and cargo-deny pins are unified on the backbone's.
   - **The Rust-core proposal's rules carry over unchanged:**
     - bytes in, typed results out;
     - no credentials, database handles or storage capabilities in Rust library calls;
     - rayon with the GIL released;
     - per-document budgets and cooperative cancellation;
     - `catch_unwind` at the bridge;
     - deterministic code-point offsets (`rendered[a:b] == block`);
     - shadow mode before per-format cutover;
     - permissive licences only.
   - **Process change.** The open PRs stay draft in Copilot and are re-opened against the backbone. Their issues remain the tracking units, re-pointed at the backbone home. Copilot's ADR-0025 evaluation and the #670 fidelity benchmark gate each format's cutover.
   - **One deliberate difference from the Rust-core proposal.** `bb-worker` is an existing backbone service that reads its queue from Postgres and fetches files through signed, expiring, allow-listed grants. Running native jobs there is accepted; it is not "a new service" in that proposal's sense.
3. **Adopt the Rust the backbone already ships.** `bb-gateway` carries all realtime traffic, so long-lived sockets leave the Python API. `bb-worker` runs native extraction and thumbnails. `backbone_native` replaces Copilot's Python parsers and chunker per format, after parity.
4. **New Rust is added only in the backbone, and only after a profile.** These candidates come from code reading; none is measured, and none ships without a before/after benchmark:
   - **Context fitter.** `fit_transcript` runs before each of up to 20 turns and rebuilds a `ToolNameMap` per message (`llm/context.py:491–529`).
   - **Embedding pipeline.** Per-coordinate validation and NDJSON encoding of 2,048-float vectors (`llm/gateway.py:350–368`, `search/store.py:657–712`).
   - **Gateway AG-UI control.** Assistant control over `bb-gateway` (U10). This is a proposal to the backbone owner only: the backbone's working agreement keeps AG-UI on the in-process hub as application code.
   - **Snippet redaction.** Aho-Corasick redaction to replace the O(W²) window scan in `tasks/summarize.py:323`. Fix the algorithm first.
   - **Shared egress guard.** One SSRF guard exposed to Python (U7).
   - **Web extraction.** HTML, feed and sitemap extraction.
5. **Stays Python:**
   - the HTTP API and routers;
   - authentication, tenancy and permission *decisions* (OpenFGA evaluates them; the INV-2 chokepoint stays small and reviewable);
   - the agent loop, which waits on models and tools;
   - product services;
   - MCP, OAuth and web search, which are network-bound;
   - the sandbox runner, which is Docker-bound.

### 6. Upstream backbone work

Status is as verified on 2026-10-24 against `saas-backbone@12b81e9` (§10). Paths are relative to `packages/backbone/src/backbone/` unless they start with `crates/`. Open items are filed in the backbone repository in package 0 and block the package named. Items already present need no upstream work.

| # | Capability | Status | Blocks |
|---|---|---|---|
| U1 | OpenSearch search engine (§4) | **Missing.** Only `search/adapters/postgres.py` and `search/adapters/meilisearch.py` exist. | R |
| U2 | Relocated Rust document core (§5.2) | **Missing.** The crates are `bb-common`, `bb-extract`, `bb-gateway`, `bb-jobs`, `bb-py` and `bb-worker`. | D (per-format cutover) |
| U3 | Assistant | **Partial.** Present: streamed tool-call arguments (`assistant/runner.py:938`), history compaction (`assistant/compaction.py`), runs as worker jobs, org and user memories (`assistant/models.py:186`), tool grants, org-managed MCP servers. Missing: tenant-authored, versioned agents (agents are code contributions only, `assistant/agents.py:92`), a narration or speculative-streaming hook, and a transcript read-back tool. | A |
| U4 | LLM per-tenant keys and prompt caching | **Present.** Per-tenant BYOK keys from `SecretVault` (`llm/settings.py:52–55`, `llm/adapters/litellm_provider.py`), cache-control injection (`litellm_provider.py:276`) and cached-token usage (`llm/streaming.py:92`). Package A checks parity with Copilot's OpenRouter cache-write accounting (#759). | — |
| U5 | Connectors | **Partial.** A full framework exists in `connectors/` (SDK, OAuth, sync, webhooks, circuit breaker, network guard), with built-in Google Drive and Slack connectors in `connectors_builtin/`. Remaining: converge on Copilot's ACL mirroring with a freshness window, and its conformance kit. | E |
| U6 | MCP client | **Present.** `assistant/mcp.py`: streamable HTTP, org-managed servers in `assistant_mcp_servers`, public-address pinning, credentials from the vault. Package E maps ADR-0012's "discovered tools default to T2" onto tool grants. | — |
| U7 | Shared egress/SSRF guard | **Partial.** Python guards exist in `connectors/network.py` (`blocked`, `public_addresses`, pinning at the TCP boundary) and in `assistant/mcp.py`; a Rust guard is in `crates/bb-worker/src/handlers/network.rs`. Remaining: expose one guard as a port, so product modules (web search, provider catalog) don't import another module's internals. | E |
| U8 | Append-only audit for the app role | **Present.** Migration `20261002_b41e9d02a611_audit_immutability` (append-only trigger and revokes) and `migrations/security.py:142`. | — |
| U9 | Human-only approval decisions | **Present.** `approvals/service.py:314` refuses any principal whose kind is not `user`. API keys, service accounts and system principals are distinct kinds (`kernel/context.py:20`). | — |
| U10 | `bb-gateway` AG-UI assistant control | **Missing, and against a backbone rule.** The gateway serves only `/health`, `/metrics`, `/realtime/ws` and `/realtime/sse` (`crates/bb-gateway/src/app.rs:122–125`), and the backbone keeps AG-UI on the in-process hub as application code. A proposal to the backbone owner, not a dependency. | — (A uses the API WebSocket) |
| U11 | Multipart presigned uploads | **Present.** `files/multipart.py` and `files/cleanup.py`. | — |
| U12 | Readiness beyond Postgres | **Missing.** `/ready` pings only the database (`web/app.py:141–146`). | T |

### 7. Invariants (spec 0004) after the migration

The platform carries mechanisms; it does not satisfy an invariant by being adopted. Every row keeps its negative-test category.

| Invariant | Mechanism after migration | Negative proof |
|---|---|---|
| INV-1 tenancy | `TenantMixin` + FORCE RLS under a non-owner role, plus explicit tenant filters in product repositories | cross-tenant → 404 on every product route, list, aggregate, search, channel and job |
| INV-2 permission | `copilot.fga` (owner, user and group grants, collection and source parents, connector-mirrored principals); `ParentPolicy`; `IndexSpec` ACLs + `post_check`; hydration re-check | unauthorized passage excluded; direct fetch → 404; revocation honoured mid-stream and on replay |
| INV-3 citations | product `AnswerValidator`: every citation is a permitted, retrieved passage with exact offsets; `citation_access` re-checks at read time | an uncited or forbidden citation is blocked |
| INV-4 authn | backbone `identity` | missing or expired credential → 401 |
| INV-5 authz | org membership roles + product FGA relations (`member`, `admin`, `security`); `require_dep` at routes | wrong role → 403 |
| INV-6 audit | `uow.audit` in the business transaction; `AuditLog.record` for denials; `AuditAction` envelope validation; the backbone's append-only audit trigger (U8) | a missing audit event fails the test; app-role `UPDATE`/`DELETE` on audit rows is rejected |
| INV-7 read-before-write | `actions` (`effect`, `confirm`) + `approvals`; the product tier gate checks the tier, and `approvals` already refuses non-human approvers (U9). Generic confirmation is never the approval. | an unapproved T2+ action is forbidden; a tool cannot approve itself |
| INV-8 input/state | `kernel.errors` problem codes | malformed input → 422; illegal transition → 409 |

### 8. Supersession scope

Behavioural requirements in the earlier ADRs survive; only the mechanisms named here change.

| ADR | Superseded portion | What survives |
|---|---|---|
| 0003 | Python 3.12 pin, Redis as broker and backplane, MinIO as *the* object store, Celery, the Copilot-owned LiteLLM gateway | FastAPI (via `backbone.web`), React SPA, Postgres, OSS-only, LLM-agnostic (OpenRouter first), WebSocket streaming, answer-quality commitments |
| 0004 | Boundary-table owners for LLM, relational database plumbing, object storage, background jobs, identity and tenant, realtime, config and secrets | One-way layering inside product modules; "one owner per external concern", where the owner is now a backbone module |
| 0005 | The service list and Celery/Redis topology | One-command local stack, isolated ports and project names |
| 0010 | §3 ownership of the OpenSearch client by `backend/app/search/` | Engine choice, single store, permission and parity rules, publication semantics |
| 0015 | §1 celery-redbeat and §4 the Celery `run_assistant` task | The data model (schedules, runs, run steps), headless-run semantics, overlap and retry rules |
| 0022 | Group and grant tables as the storage of record | Group principals, the derived "All members" group, source-visibility semantics |
| 0026 (CI) | On `backbone-migration` only: the backend job runs in the pinned backbone runtime image at its Python version (instead of setup-python 3.12.5), and pulls private images with a read-only credential. That is the one exception to "no secrets", and it ends when the backbone is OSI-published (§9). | Every gate (lint, format, typecheck, offline tests, frontend build), locked dependencies, the same commands locally and in CI, and no secrets on `main` |

ADRs 0006–0009, 0011–0014, 0016–0021 and 0023–0025 remain binding, as does 0026 outside the migration branch, except where §§2–5 move a mechanism behind a backbone port.

**`AGENTS.md`.** §3, §6, §11 and §12 change. Under §5 that needs owner approval, so this ADR ships a review-only diff: [`0030-proposed-agents-md.diff`](0030-proposed-agents-md.diff).

- **When it applies.** Once approved, the diff is applied on `backbone-migration` in package F and reaches `main` with the cutover. Applying it to `main` earlier would describe a stack `main` does not run.
- **Nested contracts.** `backend/AGENTS.md` and `contracts/AGENTS.md` are proposed in package F.

### 9. Migration plan

**Branch model.** Work happens on a long-lived integration branch, `backbone-migration`. This is a recorded exception to AGENTS.md §11's short-lived branches.

- **Package PRs.** Each package lands there by PR, with `Closes #N`.
- **Cutover.** One PR merges `backbone-migration` into `main` after package T.
- **Schemas.** Because Copilot holds only demo data, the 46 linear migrations are replaced with per-module branches and a fresh seed. There is no in-place data migration.
- **During the migration.** Product work on `main` continues. Platform-layer changes on `main` are frozen once F starts. Product changes that land on `main` meanwhile are ported by the package that owns their area.

**Packages.** They run serially (shared seams), each with frozen contracts and Docker-only checks against the minimum set of services, with round-trip read-back and teardown:

| Pkg | Scope | Exit gate |
|---|---|---|
| **0** | This ADR; owner decisions; §10 verification (done 2026-10-24); upstream issues for the open items in §6; re-point #661 | ADR accepted, `AGENTS.md` diff approved |
| **F** | Composition, image, compose, identity/tenancy/authz, `copilot.fga` draft, unit of work and app role, settings, errors, secrets, audit. Delete `core/`, `auth/` and the `db/` plumbing. T scaffold and the no-duplicate guard. | INV-1, INV-4, INV-5 and INV-6 negatives green on the new stack, including app-role `UPDATE`/`DELETE` on audit rows rejected |
| **J** | 12 Celery tasks → `@task`; RedBeat → backbone scheduler; backplane → `RealtimePublisher` + `bb-gateway` | idempotent enqueue; schedules fire; stream resume from a sequence number |
| **D** | `ObjectStore` → `FileAccess`; ingestion on the files pipeline + `backbone_native`; media queue; Rust core per format in shadow mode | multipart upload parity check; per-format parity on the ADR-0025 evaluation |
| **R** | U1, then `retrieval/` on `Searcher`; delete `search/` | INV-2 negatives incl. revocation; quality and latency ≥ `main` |
| **A** | Chat runtime → backbone `assistant` (agents, actions, approvals, `AnswerValidator`, context hooks); runs on workers; assistants as data-defined agents | U3; INV-3, INV-7; chat latency harness (#486) p50 time-to-first-answer-token not worse than `main` |
| **E** | Connectors, MCP, sandbox, web search, schedules and runs as product modules on backbone ports | U5 and U7, or product modules until they land; connector conformance kit green |
| **W** | Frontend auth/org shell and AG-UI chat transport; regenerated client | required before cutover, since `main` must keep working |
| **T** | INV-1…INV-8 matrix; no-duplicate-platform guard; contracts regenerated; local/CI parity | the cutover PR |

**The no-duplicate guard** (a smoke check from F onward) fails if `backend/` does any of these:
- imports `celery`, `redbeat`, `litellm`, `aioboto3`, `jwt` or `argon2`, or a Redis client;
- defines a WebSocket endpoint;
- defines a Cargo workspace or a PyO3 module.

From package A onward it also fails if `backend/` runs its own agent loop outside the backbone assistant runner.

**Consumption and licensing.** ADR-0003 is open-source-only, and Copilot is public under MIT.

- **Bridge.** Until the backbone is published under an OSI licence, the migration branch and its CI consume private, digest-pinned images from a registry, using a read-only pull credential held as a CI secret. This is the only exception to ADR-0026's no-secrets rule, and it never reaches `main`.
- **Cutover precondition.** Merging into `main` requires the backbone to be published under an OSI licence, so that the public repository stays buildable by anyone and `main`'s CI keeps needing no secrets. Only the owner can waive this, in writing on #716.
- **Pin.** The backbone commit and image digests are pinned in one place and bumped by PR.

### 10. Verification against the backbone source

Checked on 2026-10-24 against `sual-ai/saas-backbone@12b81e9`, unchanged since 2026-10-02. Paths are relative to `packages/backbone/src/backbone/` unless they start with `crates/` or `deploy/`.

| Question | Finding |
|---|---|
| Modules and contribution names | Modules: actions, analytics, approvals, assistant, audio, audit, authz, automations, cache, config, connectors, connectors_builtin, consent, customfields, docgen, domains, events, extract, feedback, files, flags, history, i18n, identity, imports, jobs, llm, migrations, notifications, observability, privacy, realtime, search, secrets, tenancy, usage, webhooks, plus `kernel` and `web`. Every contribution key this ADR relies on exists: `tasks`, `schedules`, `search_indexes`, `authz_models`, `resource_parent_policies`, `assistant_agents`, `context_providers`, `actions`, `privacy_handlers`, `connectors`. |
| Ports named in §§1–4 and §7 | All exist: the function-scoped `Uow` (`web/deps.py:110`); the non-owner `create-app-role` with `NOBYPASSRLS` (`migrations/security.py:124`) and `backbone doctor` (`cli.py:484`); the `app.tenant_id` GUC (`kernel/uow.py:94`); `JobQueue.enqueue` and `Schedule` (`jobs/ports.py:155, 73`); `RealtimePublisher` and `TicketIssuer` (`realtime/ports.py:32, 58`); `IndexSpec(acl_from, acl_parents, embedding)` (`search/ports.py`); `uow.audit` (`kernel/uow.py:130`) and `AuditLog.record`/`verify` (`audit/ports.py:88, 101`); `ParentPolicy` (`authz/ports.py:81`); `AnswerValidator` and `ProcessingContextResolver` (`assistant/ports.py:181, 65`); tenancy groups (`tenancy/models.py:131`, `group_member_ids`); `SecretVault` (`secrets/ports.py:37`); `audio` transcription (`audio/ports.py:65`); `ActionDef.effect`/`confirm` (`actions/ports.py:72–81`). |
| Search post-check | `post_check` defaults to off (`search/settings.py:73`). Copilot must turn it on (INV-2). |
| Python and Rust versions | Python 3.13 (`deploy/docker/python.Dockerfile`) and Rust 1.90 (the backbone working agreement). |
| S3 adapter against MinIO | Files use `obstore`, built on the Rust `object_store` crate, for any S3-compatible store, path-style by default (`files/adapters/obstore_store.py`, `files/settings.py:25`). The reference stack uses SeaweedFS. MinIO should work but is untested upstream, so package F runs a real put, get and presign check before keeping it. |
| Incomplete uploads | Multipart sessions are tracked and cleaned up (`files/multipart.py`, `files/cleanup.py`), so Copilot's MinIO multipart reaper can go. |
| Where assistant runs execute | On the worker, as `assistant.run` jobs on the `assistant` queue (`assistant/tasks.py:11`, `assistant/execution.py:52`). This confirms the admission-control and session-lifetime claims in Context. |
| Observability | OpenTelemetry traces and metrics with ASGI, httpx and SQLAlchemy instrumentation, trace propagation, and optional OTLP and Sentry exporters (`observability/`). This covers Copilot's missing metrics and traces. |
| Licence | No licence file, and the Rust workspace declares `UNLICENSED`. The backbone is not OSI-licensed, so the §9 cutover precondition stands. |
| Upstream items | See the status column in §6: five items already exist (U4, U6, U8, U9, U11), three are partial (U3, U5, U7), three are missing (U1, U2, U12), and U10 conflicts with a backbone design rule. |

## Consequences

- **Less code, inherited fixes.** About 11k lines of security-sensitive platform code leave Copilot. Fixes to auth, RLS, audit, queueing and transport arrive by bumping a pin instead of being rebuilt. The accountant and Copilot converge on one methodology and one platform.
- **More services.** The runtime gains OpenFGA, two Rust services and a scheduler, and loses Celery, RedBeat and Redis-as-broker. Local memory use changes; package F records the new footprint.
- **Better structural defaults.** Runs move off the API process, fixing admission control and the database session held across model latency. Streams gain sequence resume. Jobs gain idempotency keys. The permission predicate is written once (OpenFGA) instead of twice.
- **Rust grows in one place.** It grows in the backbone, benefits every product, and is measured before it grows further. Copilot's #661 contributors work in the backbone repository from now on.
- **Cost of the migration.** The migration is large: nine packages, serial, with a platform freeze on `main` once F starts. Copilot's velocity on new platform features drops during that window. Product work continues but carries porting cost.
- **External dependencies.** Some Copilot capabilities depend on upstream work still open after verification (U1, U2, U3, U5, U7, U12). If an item stalls, the dependent surface stays disabled on the migration branch; a local copy is never the fallback.
- **Wire changes.** The API contract changes (auth, org selection, AG-UI chat events, problem codes, pagination). The frontend package W is therefore mandatory before cutover, even though this program is backend-first.
- **Public buildability.** Until the backbone is OSI-published, outside contributors cannot build the migration branch.

## Alternatives considered

- **Keep Copilot's own platform and only share code by copy.** Rejected. It keeps two diverging implementations of every security-sensitive concern and is the status quo the owner asked to leave.
- **Strangler migration on `main` (backbone modules beside Copilot's own).** Rejected. It requires running two auth, queue and socket platforms at once, which §2 forbids. Demo-only data removes the reason to strangle.
- **Switch retrieval to backbone Postgres or Meilisearch.** Rejected for now. It would reopen ADR-0010 and needs a retrieval-quality bake-off, while contributing OpenSearch upstream gives the backbone a large-corpus engine.
- **Keep the Rust core in Copilot (`rust/`) and upstream later.** Rejected. Two PyO3 extensions and two toolchains in one process, plus a later migration of the same code.
- **Rewrite the API or agent loop in Rust.** Rejected. Both are I/O-bound and measured fast; the risk to INV-1…INV-8 is high and the gain unproven.

## Resolved decisions (owner, 2026-10-09)

1. **Adopt SaaS Backbone as Copilot's platform layer.** Use the accountant precedent and method.
2. **Retrieval:** keep OpenSearch, contributed upstream as a backbone engine (§4, U1).
3. **Rust:** relocate the #661 Rust document core into the backbone. Hold #701–#715 in Copilot (§5).
4. **Agent runtime:** product hooks on backbone ports first; generic pieces upstream one per change (§2, U3).
5. **Data:** demo data only, so replace schemas outright on `backbone-migration` (§9).
6. **Licensing:** publish the backbone under an OSI licence; private pinned images are the bridge until then (§9).
