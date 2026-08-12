# 9. Connector framework + Web URL source (first connector)

- **Status:** Accepted *(connector type decided by sponsor 2026-06-23; framework design proposed)*
- **Date:** 2026-06-23
- **Builds on:** [ADR-0004](0004-architecture-boundaries-and-adapters.md) (the `connectors/` boundary), [ADR-0006](0006-contract-first-parallel-implementation.md), [ADR-0008](0008-conflict-free-parallel-delivery.md), [spec 0004](../specs/0004-security-and-domain-invariants.md) (ACL mirroring, read-before-write tiers)

## Context

**Sources** is the 7th wireframe surface ([ADR-0007](0007-adopt-wireframe-ia-and-design-system.md)) and the only one not yet built — it needs a **connector framework** (the `backend/app/connectors/<name>/` boundary, [AGENTS.md §6](../../AGENTS.md)). Real third-party connectors (Drive, Slack, Confluence…) each require source-side app registration (OAuth client IDs, scopes, admin consent) and per-source ACL mirroring — a large, per-connector decision.

**Sponsor decision (2026-06-23):** the first connector must be one a user can add **with zero source-side setup** — no app IDs, no OAuth app creation, no credentials. That selects a **Web URL** connector: paste a public link, we ingest it.

## Decision

1. **Connector framework** in `backend/app/connectors/`. A `Connector` protocol — `validate_config(config)`, `sync(source) -> Iterable[FetchedDoc]`, `health(source)` — and an **auto-discovered registry** (the [ADR-0008 §3](0008-conflict-free-parallel-delivery.md) scan pattern, so adding a connector touches only `connectors/<name>/`). Connectors expose **domain types only** ([ADR-0004](0004-architecture-boundaries-and-adapters.md)); they never leak vendor SDK types.

2. **First connector — `web`.** The user provides a public URL:
   - a **single page** → one document; or
   - an **RSS/Atom feed** or **sitemap.xml** → many documents (bounded count).
   The connector fetches over HTTP(S), extracts readable text (HTML → text; plain text; `text/*`), and feeds the **existing ingestion pipeline** (#21: parse → chunk → embed → pgvector). Ingested content is **tenant- and owner-scoped, deny-by-default** — only the adding user (within their tenant) can retrieve it (INV-1/INV-2). Re-sync re-fetches and re-indexes.

3. **SSRF defense is mandatory and load-bearing** (`risk:security`). The server fetches user-supplied URLs, so the connector MUST:
   - allow only `http`/`https` schemes;
   - **resolve the host and reject** loopback, private (RFC-1918), link-local, CGNAT, and cloud-metadata ranges (e.g. `169.254.169.254`), incl. on **every redirect hop** (re-validate after each redirect; do not follow to a blocked target);
   - cap response **size** and **time**, and allowlist content types;
   - **require a `2xx` final status** — a non-2xx response (e.g. a UA-gated `403` *page*) is a failed fetch surfaced as `fetch_failed`, never decoded and ingested as content (#138);
   - send a **descriptive `User-Agent`** (`WEB_USER_AGENT`, version-stamped by default) — a UA-less fetch is rejected by many sites (e.g. Wikimedia) with an error page (#138);
   - apply a per-tenant fetch **rate limit**.
   These checks live in one `connectors/web/fetch.py` chokepoint with explicit negative tests. A bypass is a blocking defect.

4. **Sources data model.** A tenant/owner-scoped `sources` table: `id, tenant_id, owner_id, type, config (jsonb: url, mode), status (pending|syncing|ready|error), last_synced_at, indexed_count, last_error`. Ingested documents link back via `source_id`. Sync runs as a **Celery task** (#21 tasks), never in the request path. Source health is an aggregate of document truth: `ready` is permitted only when every document returned by the completed sync is itself `ready` for its current ingestion attempt. Any Failed or otherwise non-Ready document makes the source `error`; `indexed_count` counts only Ready documents and `last_error` records a content-safe failure summary. Partial success is therefore visible as Error with a non-zero Ready count, never falsely promoted to Ready.

5. **Sources surface (contract, frozen first per [ADR-0006](0006-contract-first-parallel-implementation.md)):**
   - `GET /sources` — connector grid: per-source type, sync health/status, `indexed_count`, permission/owner.
   - `POST /sources` — add a source (`type: web`, `url`); validates + SSRF-checks the URL, enqueues the first sync. **A write → read-before-write tier** (T1, owner-gated; spec 0004).
   - `POST /sources/{id}/sync` — re-sync. `DELETE /sources/{id}` — remove (cascades its docs).
   - Every add/sync/delete emits an **audit** event.

6. **Deferred (separate decisions, not this scope):** OAuth/third-party connectors (Drive/Slack/Confluence/etc.) — each needs source-side app registration + per-source ACL mirroring and its own ADR.

## Consequences

- The Sources surface lights up **end-to-end with zero source-side setup**: add a URL → sync → it's searchable and chat-groundable like an upload.
- SSRF — the one real risk of server-side fetch — is handled **once** at the framework boundary, with negative tests, before any second connector exists.
- Future connectors are **additive** via the auto-discovered registry; the `sources` table, sync-health model, and Sources screen are connector-agnostic.
- Delivery follows the M2 shape: serialized prep (this ADR + `/sources` contract + the `sources` migration) → parallel build (web connector BE ‖ Sources screen FE), per [ADR-0008](0008-conflict-free-parallel-delivery.md).
