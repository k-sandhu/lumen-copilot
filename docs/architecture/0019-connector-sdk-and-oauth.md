# 19. Connector SDK, OAuth & ACL mirroring (first managed connector: Google Drive)

- **Status:** Proposed *(spike [#290](https://github.com/k-sandhu/lumen-copilot/issues/290) deliverable; sponsor committed epic [#289](https://github.com/k-sandhu/lumen-copilot/issues/289) and picked Google Drive as the first managed connector, 2026-07-18)*
- **Date:** 2026-07-18
- **Builds on:** [ADR-0004](0004-architecture-boundaries-and-adapters.md) (boundaries; prefer an HTTP boundary to a vendor SDK), [ADR-0009](0009-connector-framework-and-web-source.md) (connector framework, the `sources` table, the SSRF/egress discipline), [ADR-0010](0010-dedicated-text-search-engine.md) (OpenSearch single retrieval store; the engine-side permission filter), [ADR-0012](0012-mcp-integration.md) (the `auth_ref`-into-CC-C pattern; the guarded egress client), [spec 0004](../specs/0004-security-and-domain-invariants.md) (§2.2 decides the mirrored-ACL model; INV-1/2/4/5/6/8), CC-C secrets vault ([#209](https://github.com/k-sandhu/lumen-copilot/issues/209))
- **Scope:** SPIKE — this ADR is the **only** deliverable of [#290](https://github.com/k-sandhu/lumen-copilot/issues/290); no product code, no migrations. It fixes the load-bearing decisions and sketches data-model/contract shapes as **guidance** for the epic's features (F-CB-0…F-CB-5, filed from [#289](https://github.com/k-sandhu/lumen-copilot/issues/289)).
- **Tracking:** [#290](https://github.com/k-sandhu/lumen-copilot/issues/290) (this spike) · epic [#289](https://github.com/k-sandhu/lumen-copilot/issues/289)

## Context

[ADR-0009](0009-connector-framework-and-web-source.md) shipped the connector *framework* — the `Connector` protocol (`validate_config` / `sync` / `health`), the auto-discovered registry, the tenant/owner-scoped `sources` table, the Celery sync task, and one connector (`web`, unauthenticated, public content, owner-only visibility) — and **deliberately deferred** everything a real enterprise connector needs: authenticating to a source, mirroring per-item ACLs, and syncing incrementally. The sponsor has now committed epic [#289](https://github.com/k-sandhu/lumen-copilot/issues/289) (Connector Breadth v1) and chosen **Google Drive** as the first managed connector.

What already exists and is reused, not re-derived:

- **Egress discipline in one place:** `backend/app/net/egress.py` (the shared SSRF primitive: blocked-range checks, resolve-all/reject-any, IP-pinning) with two consumers — `connectors/web/fetch.py` and the MCP guarded httpx client (`backend/app/mcp/egress.py`, ADR-0012 §4).
- **Credentials in one place:** the CC-C secrets vault ([#209](https://github.com/k-sandhu/lumen-copilot/issues/209)) — envelope-encrypted, write-only, plaintext obtainable only in-process via `secrets_service.get_secret_plaintext(...)`; `mcp_servers.auth_ref` (ADR-0012 §5) is the recorded pattern for "a row references a credential, never holds one".
- **Permission enforcement in two lockstepped chokepoints** (ADR-0010 §4): the Postgres predicate (`retrieval/queries.py::_document_permitted` — `tenant AND (owner ∈ owners OR grant EXISTS)`) and the engine filter (`search/filters.py::SearchAllowFilter` — mandatory `tenant_id` term + owner/granted-doc/granted-collection terms), with Postgres hydration re-checks as defense in depth. The comment in `retrieval/permissions.py` already names "mirrored connector ACLs" as the deferred widening of this exact seam.
- **The decided ACL model** ([spec 0004](../specs/0004-security-and-domain-invariants.md) §2.2, *not re-litigated here*): connector documents store a **mirrored ACL principal-set** normalized to tenant principal IDs, refreshed on sync; retrieval intersects the requester's principals with the document's set; **stale or unknown ACL ⇒ deny** (configurable freshness window); the system never grants access the source would deny.

There is **no OAuth machinery anywhere in the app today** (grep-verified) — the authorization-code flow, state handling, and token storage designed here are greenfield, built once at the framework level so the second managed connector reuses them.

## Decision

### 1. Source authentication — OAuth 2.0 authorization-code + PKCE, initiated by a tenant admin; tokens live in CC-C

- **Flow.** Authorization-code with **PKCE (S256)** and offline access (refresh token). A connector that needs OAuth declares it via the SDK (§4); the **framework** owns the generic machinery (`connectors/oauth.py`: authorize-URL construction, state, PKCE, code exchange, token refresh) — nothing Google-specific leaks out of `connectors/gdrive/`.
- **Who may connect: tenant admins only** (`Role.ADMIN`, enforced via the existing `require_roles` dependency — INV-5). Connecting a managed source imports organization content *and* its ACLs; that is an admin trust decision, unlike the owner-scoped `web` source. Non-admin connect attempt → **403** (mandatory negative test).
- **Endpoints (contract frozen in F-CB-0, shape fixed here):**
  - `POST /sources/{id}/connect` *(admin)* → `{authorization_url}` for a source row in the new `pending_auth` status.
  - `GET /sources/oauth/callback?state&code` — the **one** redirect-target endpoint, shared by all OAuth connectors. It is unauthenticated in the JWT sense (a browser redirect carries no bearer header) and instead **authenticated by `state`**: a server-issued, HMAC-signed, **single-use** token (Redis-tracked jti, TTL ≤ 10 min) binding `{tenant_id, user_id, source_id, code_verifier}`. Missing/expired/replayed/forged state → **fail closed, no code exchange** (INV-4/INV-8 negative tests). On success the server exchanges the code, stores tokens (below), marks the source `pending`, enqueues the first sync, and 302-redirects to the Sources screen.
- **Token storage.** The **refresh token** is stored via `SecretsService.store_secret` under a new `SecretKind.CONNECTOR_OAUTH` (`connector_oauth`), owned by the connecting admin; the `sources` row gains **`auth_ref`** (nullable UUID → the CC-C secret), mirroring `mcp_servers.auth_ref`. **Access tokens are never persisted** — the sync task obtains one per run via the refresh grant and holds it in memory only. Token material never enters `sources.config`, logs, audit metadata, or any API response.
- **Token use & refresh.** The Celery sync task reads the refresh token via `get_secret_plaintext(auth_ref, accessor=AuditActor.system())` (the exact MCP/LLM-provider adapter pattern, so `secret.accessed` names the reader). A rotated refresh token is re-stored in place (the vault upsert keeps the handle stable). A dead grant (`invalid_grant` — revoked/expired) fails the sync closed: source `status=error`, health reports **reauthorize required**; re-connect runs the flow again onto the same row.
- **OAuth client credentials** (the platform's Google app registration: client id/secret) are **deployment-level config** via `core/config.py` (`GDRIVE_OAUTH_CLIENT_ID` / `GDRIVE_OAUTH_CLIENT_SECRET`, blank-refused outside `local` per the config conventions). Per-tenant bring-your-own-client is a recorded follow-up, not v1.
- **Deleting a source** (or the connector being disconnected) deletes the referenced secret via the vault (audited `secret.deleted`) in the same operation that removes the row and its documents — no orphaned credentials (the #139/#269 reconcile lesson applied to secrets).
- **Audit (additive, INV-6):** new `source.connected` action (records the source, the acting admin, the connected account email + granted scopes — never tokens), joining the existing `source.added/synced/deleted`; `domain/audit.py` + the exact-set pin in `tests/test_audit_taxonomy.py` update in lockstep in F-CB-1.

### 2. ACL mirroring — the concrete contract (implements spec 0004 §2.2)

- **Domain shape.** A capability-declaring connector returns, per fetched document, a **mirrored principal-set** — the source's allow-list normalized to Lumen principals. Principal vocabulary (v1): `user:<lumen_user_uuid>` and `tenant` (tenant-wide). The mapping (`map_acl`, §4) is **pure and fail-closed**: anything it cannot map **grants nobody**.
- **Identity mapping (source user ↔ Lumen user): by verified email**, case-folded, within the connecting tenant only. For Drive: a `type=user` permission maps iff its email matches a tenant user's email; `type=domain` maps to `tenant` **iff** the domain equals the connected account's workspace domain (captured at connect time from the token's account info); `type=anyone` (public link) maps to `tenant` (a tenant-wide grant is a strict subset of "anyone" — *never escalates* holds); **`type=group` is not expanded in v1** (group expansion needs the Directory API / SCIM — CC-3 v2 territory, epic scope fence) and therefore maps to nothing ⇒ **deny**. Under-sharing is the deliberate failure direction.
- **Fail-closed rules:**
  - a permissions fetch that errors ⇒ that document is **skipped this sync** (never ingested/kept with unknown rights);
  - a document whose mapped set is **empty** is ingested but retrievable by **no one** via the ACL leg (it lights up when mapping improves), and sync health counts these (`unmapped_acl_count`) so an admin can see silent-deny volume;
  - a mirrored ACL older than the freshness window (`CONNECTOR_ACL_MAX_AGE_HOURS`, default 24) ⇒ **deny** — a stalled sync progressively hides connector content rather than serving stale rights.
- **Storage.** Postgres (source of truth): `documents` gains `acl_principals` (jsonb array, null = "no mirrored ACL — not a connector-ACL document") and `acl_synced_at`. OpenSearch: the chunk doc + strict mapping gain `acl_principals` (keyword array) and `acl_synced_at` (date) — touched in `IndexedChunk`, `_index_body`, the bulk writer, and `index_sync._to_indexed`; requires a mapping bump + the existing idempotent reindex path.
- **Enforcement — widen both chokepoints in lockstep, nowhere else:**
  - Postgres `_document_permitted` gains an ACL leg: `owner ∈ owners OR grant EXISTS OR (acl_principals ⊇ {requester-principal} AND acl_synced_at ≥ now() − window)`;
  - `SearchAllowFilter.to_engine_filter()` gains the matching `terms` clause on `acl_principals` (requester's `user:<id>` + `tenant`) guarded by an `acl_synced_at` range — inside the existing `minimum_should_match:1` bool, so the mandatory `tenant_id` term is untouched (INV-1);
  - hydration re-checks (ADR-0010 §4) re-evaluate the same widened predicate in Postgres before anything becomes a citation — the engine is never solely trusted.
  - The **owner leg is retained** for connector documents (owner = the connecting admin): the sync identity can, by construction, already read in the source everything it syncs, so owner-leg visibility never exceeds source access.
- **Refresh cadence.** ACLs refresh on **every** sync — full or incremental (Drive's change feed flags permission changes); `acl_synced_at` advances per document per sync. A source-side revocation is enforced at the next sync, and no later than the freshness window (the window is the recorded worst-case revocation-to-enforcement bound).

### 3. Change detection — cursor-based incremental sync (polling; webhooks stay out)

- **Framework interface.** An optional capability: `fetch_changes(source, cursor) -> SyncDelta` where `SyncDelta` = `{upserts: Iterable[FetchedDoc], deleted_external_ids: frozenset[str], next_cursor: str}`. Full `sync()` remains mandatory (bootstrap, and the fallback when a cursor is rejected). The `sources` row gains **`sync_cursor`** (text, null = next sync is full). `FetchedDoc` widens **additively** (defaults keep the `web` connector untouched): `external_id`, `modified_at`, `acl` (the §2 principal-set).
- **Reconcile by identity, not wholesale.** With `external_id` present, the sync task upserts by `(source_id, external_id)` and deletes by `deleted_external_ids` — replacing the current delete-all-then-reingest reconcile for incremental runs (full runs keep it). `documents` gains `external_id` (nullable; unique per source when set).
- **Drive concretely:** `changes/startPageToken` captured at the first full sync; `changes.list` pages thereafter (files + permission changes + removals); an invalid/expired cursor (HTTP 410) clears `sync_cursor` and falls back to a full resync.
- **Cadence:** on-demand (`POST /sources/{id}/sync`, unchanged) plus a periodic poll per connected source via the existing Celery beat, interval `CONNECTOR_SYNC_INTERVAL_MINUTES` (default 60), always through the existing enqueue seam and its per-tenant rate limit. **Webhooks/push change notification is E7-2 and stays out** (epic scope fence).

### 4. Connector SDK — in-process capability protocols + a conformance kit

- **Trust model: in-process, first-party only.** v1 connectors are in-repo, code-reviewed Python under `backend/app/connectors/<name>/` — the same trust boundary as the rest of the backend, so **no ADR-0013 sandbox is required**. This holds **only** while every connector is first-party: third-party / push / SDK-only custom connectors (out of scope, epic fence) must revisit isolation *before* any foreign code loads — recorded here so nobody adds one in-process later.
- **SDK = the ADR-0009 base protocol + optional capabilities**, all discovered from the connector object (no registry changes; the ADR-0008 scan pattern is untouched):
  - base (mandatory): `name`, `validate_config`, `sync`, `health`;
  - `oauth_spec()` → the provider's authorize/token endpoints, scopes, and extra params — presence makes the framework drive §1's flow for this connector type;
  - `fetch_changes(source, cursor)` — presence enables §3 incremental sync;
  - `map_acl(raw) -> frozenset[str]` — presence marks documents as connector-ACL'd (§2) and **requires** the F-CB-3 negative-test kit to pass for this connector.
- **Vendor boundary:** connectors expose domain types only (ADR-0004). The Drive client is **plain `httpx` against the Drive REST v3 / OAuth endpoints — no Google SDK** (the "prefer an HTTP boundary" rule; unlike MCP there is no moving protocol here justifying an SDK, just versioned REST).
- **Conformance kit (F-CB-5):** a net-new test suite parametrized over `registered_types()` asserting, per connector: protocol surface, domain-types-only returns, typed `ConnectorError`s, and — per declared capability — cursor round-trip/fallback semantics and fail-closed ACL mapping. `web` and `gdrive` both pass it; the kit + a "build a connector" doc are the E1-5 deliverable, and the *next* managed connector is the real proof the SDK holds.

### 5. First managed connector — Google Drive, v1 scope

- **Read-only**, OAuth scope `https://www.googleapis.com/auth/drive.readonly` (content + metadata + per-file permissions). Config selects what to sync: the connected account's **My Drive**, a specific **folder**, or a **Shared Drive** (`config: {mode: my_drive|folder|shared_drive, folder_id?, drive_id?}`).
- **Content:** Google-native types exported to text (Docs → text, Sheets → CSV, Slides → text); binary formats the existing ingestion pipeline already parses (PDF, DOCX, …) downloaded within a size cap (`GDRIVE_FETCH_MAX_BYTES`); everything else skipped and counted in sync health.
- **Egress:** all traffic goes to a **fixed pinned host set** — `accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com` — over a guarded httpx client (https-only, timeouts, streamed size caps, descriptive UA), reusing `app/net/egress.py` exactly as MCP does; per-tenant rate limiting reuses the existing limiter, and Drive `429/Retry-After` backs off. There is no user-supplied URL anywhere in this connector; a request leaving the pinned host set is a defect at the ADR-0009 §3 bar.
- **`sources` additions (one migration):** `auth_ref` (uuid, null), `sync_cursor` (text, null), `connected_account` (jsonb: email + workspace domain, null); new `SourceStatus.PENDING_AUTH`; contract `SourceType` enum gains `gdrive`.
- **Health:** the existing probe surface reports token validity (a bounded `about.get`), cursor state, last ACL refresh, and the unmapped-ACL count — the connect/consent UI (F-CB-4) renders these plus **reauthorize required**.
- **Deployment prerequisite (documented in F-CB-5's docs):** a Google Cloud OAuth app registration; `drive.readonly` is a Google-**restricted** scope, so a public/production verification review applies to a real deployment — local/dev runs use test-mode credentials. This is operational cost, not architecture.

### 6. Boundary table — no new row needed

The ADR-0004 §6 row **"External source connectors → `backend/app/connectors/<name>/`"** already covers `connectors/gdrive/` — it is the single module that talks to Google (Drive *and* Google's OAuth endpoints). The generic OAuth machinery (`connectors/oauth.py`, provider-agnostic) and the callback route are framework code with no vendor specifics. No `AGENTS.md` edit is required by this ADR.

## Negative-test list (the F-CB-3 kit; spec 0004 categories)

| Invariant | Must fail closed |
|---|---|
| INV-1 | cross-tenant source/document → 404; engine filter excludes foreign-tenant chunks |
| INV-2 | requester not in mirrored ACL → passage excluded + direct fetch 404 · group-only ACL → deny · unmapped principals → deny (nobody but owner/grants) · `acl_synced_at` beyond window → deny · source-side revocation → excluded after next sync · mapped set is provably ⊆ the source allow-list (never-escalate property test) |
| INV-4/8 | callback with missing/expired/replayed/forged `state` → rejected, **no token exchange** · failed exchange → no secret row · malformed connector config → 422 |
| INV-5 | non-admin `connect` → 403 |
| INV-6 | connect/sync/delete without its audit event → fail; `secret.accessed` names the sync system actor |
| — | token material never in `sources.config`, logs, audit metadata, or any API response (grep + serialization tests) |

## Consequences

- **The moat gets real.** Permission-aware retrieval over live enterprise content — the E1-1/E1-2 promise — lands with the ACL model spec 0004 §2.2 already decided, enforced at the two existing chokepoints rather than a new one.
- **OAuth is built once.** The flow, state discipline, vault storage, and refresh handling are framework-level; the second managed connector (Slack/Confluence) implements `oauth_spec()` + `map_acl()` + `fetch_changes()` and inherits the rest — that drop-in is the SDK's success criterion.
- **Deliberate under-sharing.** Email-based identity mapping and unexpanded groups mean some legitimately-accessible documents stay invisible (counted, surfaced in health). That is the safe failure direction; group expansion via Directory/SCIM is the recorded follow-up that widens it.
- **New operational surface:** a Google OAuth app registration per deployment (restricted-scope verification for production), ACL-freshness tuning (the window is the revocation-enforcement bound), and an OpenSearch mapping bump + reindex.
- **Delivery (ADR-0008):** this ADR is the serialized seam. Then: **F-CB-0** (contract freeze: connect/callback wire + `gdrive` config/status shapes) → parallel **F-CB-1** (OAuth + vault, backend) → **F-CB-2** (Drive connector + ACL mirror + incremental sync) → **F-CB-3** (negative-test kit) ‖ **F-CB-4** (connect/consent + health UI, after F-CB-0) → **F-CB-5** (SDK docs + conformance kit). Each its own issue/PR with `Closes #`.

## Resolved decisions (sponsor 2026-07-18; remainder sponsor-delegated to this ADR)

1. **First connector: Google Drive** (sponsor, recorded on [#289](https://github.com/k-sandhu/lumen-copilot/issues/289)).
2. **AuthN:** OAuth 2.0 authorization-code + PKCE, tenant-admin-initiated; refresh token in CC-C (`SecretKind.CONNECTOR_OAUTH`, `sources.auth_ref`); access tokens in memory only; single state-authenticated callback endpoint; platform-level OAuth client config (BYO-client deferred).
3. **ACL mirror:** email-based user mapping · domain-match → tenant-wide · `anyone` → tenant-wide · groups unexpanded ⇒ deny · empty/unknown/stale (window, default 24 h) ⇒ deny · stored as `acl_principals`+`acl_synced_at` in Postgres + the OpenSearch mapping · enforced by widening `_document_permitted` and `SearchAllowFilter` in lockstep.
4. **Change detection:** polling with per-source cursors (`fetch_changes`/`SyncDelta`; Drive Changes API), identity-based reconcile via `external_id`, periodic beat + on-demand; webhooks out (E7-2).
5. **SDK:** in-process first-party capability protocols on the ADR-0009 base (`oauth_spec` / `fetch_changes` / `map_acl`); no sandbox until third-party connectors are considered; plain-HTTP vendor boundary (no Google SDK); conformance kit parametrized over registered connectors.
6. **Governance:** admin-only connect (INV-5); additive `source.connected` audit action; read-only scope — write-back stays E5/T2+, untouched.
