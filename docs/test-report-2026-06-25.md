# Test Report — Backend E2E + Frontend UI/UX (2026-06-25)

> Point-in-time QA assessment of the running Lumen Copilot stack. Not a contract or
> spec; it records what was exercised, what passed, and the defects found, so they can
> be triaged into the issue pipeline (see [WORK_TRACKING.md](WORK_TRACKING.md)).

| | |
|---|---|
| **Date** | 2026-06-25 |
| **Scope** | Comprehensive backend end-to-end test, then a frontend test focused on UI/UX |
| **Method** | Full automated suites + live E2E against the running `docker compose` stack + a live browser walkthrough of the SPA |
| **Environment** | Backend `:47181` (healthy), frontend `:47180`, Postgres+pgvector / Redis / MinIO all ready. Seed user `dev@acme.test`. **`OPENROUTER_API_KEY` blank in the running stack** (the real key lives in the host env). The running containers serve `main` (ahead of the branch the unit suites ran on). |

## 1. Verdict

The backend is **production-grade**; the frontend is **polished and well-engineered at rest**. The dominant problem spans both tiers: **the core product flow — streaming a grounded answer — is broken end-to-end**, and frontend error-handling paths are the weak spot. Two backend security/robustness issues and several UI/UX gaps round out the register in §5.

---

## 2. Backend

### 2.1 Automated suites
- **pytest: 643 passed, 0 failed, 0 errored, 10 skipped** (~6.5 min). All 10 skips are expected external-dependency / live-gated tests (Postgres-only RLS/retrieval/grants, MinIO integration, `RUN_LIVE` LLM smoke).
- **mypy `--strict`: clean** (87 files). **ruff lint: clean.**
- Nit: `ruff format --check` would reformat **1 file** (`tests/test_chat_api.py`) — cosmetic, would fail a CI format gate.

### 2.2 Live E2E across API slices

| Slice | Verdict | Notes |
|---|---|---|
| Auth / tenancy / RBAC | ✅ pass | Login, refresh-rotation + replay rejection, logout revoke, 9 token-tampering variants → 401 (incl. `alg=none`), no user-enumeration leak |
| Chat + WebSocket streaming | ⚠️ partial | REST lifecycle + WS authz solid; **streaming delivery broken (see §5 #1)** |
| Documents + ingestion | ✅ pass | Upload → 302 presigned content → delete (row + chunks + MinIO object); ingestion retries/back-off then clean terminal `failed`; never stuck |
| Search + saved-searches | ⚠️ partial | Suggest/recent/saved-searches correct & tenant-isolated; `GET /search` → 503 (LLM unconfigured) |
| Sources + connectors | ✅ pass | CRUD + sync, real egress + descriptive UA, **SSRF guard** (ftp/loopback/metadata → 422), **delete-cascade holds** (#139/#141) |
| Collections + grants | ⚠️ partial | CRUD + cross-tenant 404 + grant semantics correct; **RLS backstop inert (see §5 #2)** |
| Admin / audit / models / preferences | ✅ pass | Shapes, RBAC 403, per-user prefs, audit emission + tenant-scoped, foreign-cursor fail-closed |

### 2.3 Security invariants (verified live)
INV-1 cross-tenant → **404** (never 403/200); INV-2 ownership deny-by-default; INV-4 missing/invalid token → **401**; INV-5 wrong-role → **403** (401 before 403); INV-6 audit emission through one sink; INV-8 malformed input → **422** / fail-closed cursors. Header spoofing of tenant/role ignored (sourced from the signed token only).

---

## 3. Frontend

### 3.1 Automated gates
**TypeScript (strict): clean · ESLint (`--max-warnings 0`): clean · Vitest: 612/612 passing (97 files).** Playwright E2E not run.

### 3.2 UI/UX walkthrough — what works (live)
- **Enterprise shell**: nav rail, tenant chip, notifications/account menus; a standout **⌘K command palette** (navigation + 3 appearance modes + 7 themes + 3 densities, ESC-dismissable).
- **Chat**: user/assistant bubbles, **per-message model badges**, **sanitized markdown** (`react-markdown` + `rehype-sanitize`, not raw), citations footer that honestly handles the no-source case, per-answer 👍/👎/copy, pinned composer with a sources-scope toggle + model picker, honest "Lumen can be wrong" disclaimer.
- **Documents**: collections sidebar, drag-drop + file-picker upload, status filter chips, clean table (file-type badge, chunk count, "Private to you", owner, status), friendly empty state.
- **Sources**: KPI dashboard (connected / indexed / needs-attention / last-sync) + per-source cards with sync/remove.
- **Audit**: "provable after the fact" framing, KPIs, actor/type/resource/date filters, **CSV export**, pagination, "tamper-evident · append-only ledger".
- **Admin**: honest "read-only for v1 (read-before-write)" scoping; tenant-scoped members table with role pills; model-governance table; WAI-ARIA tabs.
- **Resilience**: session restored via refresh-cookie; **initial 503s auto-retried to 200** (TanStack Query); richly accessible DOM (landmarks, ARIA labels, list semantics).

### 3.3 Static UX-quality audit (code-level)
Strong: sanitized-markdown pipeline (sanitize applied last), most async surfaces implement loading/empty/error/streaming, cancellable stream, error boundaries around feature roots, **clean `api/` boundary** (no stray `fetch`/WebSocket, no `any`), memory-only token + single silent-refresh-and-retry, WS backoff+jitter, responsive (verified via CSS + a passing narrow-width test), `prefers-reduced-motion` honored. Defects folded into §5.

---

## 4. The headline defect — streaming a grounded answer (triangulated)

Asking a question in a new chat returned **202** with a `stream_id`, an assistant bubble appeared attributed to the model — then stayed **permanently empty: no answer, no error, no retry** (DOM: `assistant message` → "Lumen" + "Answered by …opus-4.8" + nothing). Root cause, confirmed from all three sides:

1. **Backend** — the production Redis backplane has **no replay buffer**; the producer publishes `start` + terminal envelope ~9 ms after the 202, *before* the browser can subscribe → client receives **zero envelopes**. (Offline tests use an in-memory backplane with a replay buffer, which masks this.)
2. **Frontend** — `useChatStream.ts` has **no first-token/idle watchdog**; an open-but-silent socket never resolves to an error.
3. **Live** — the blank, dead-ended assistant turn, exactly as predicted.

---

## 5. Prioritized defect register

| # | Severity | Area | Defect | Fix locus |
|---|---|---|---|---|
| 1 | **High** | BE+FE | Streaming answers never reach the client (Redis backplane publish-before-subscribe race) and the UI has no watchdog → blank dead-end | `backend/app/realtime/backplane.py` (bounded replay / Redis Streams) **and** `frontend/.../useChatStream.ts` (first-token watchdog) |
| 2 | **High** | BE | RLS backstop **inert** — app DB role is `SUPERUSER`/`BYPASSRLS`, so tenant isolation rests only on app-layer predicates (no DB safety net). App-layer isolation itself verified correct | Run app under a non-superuser, non-`BYPASSRLS` Postgres role |
| 3 | **High** | FE | Error paths wedge the renderer; **Search 503 = silent blank pane** (no message, no retry); WS not closed on `done` → real client auto-reconnects a completed stream | FE error states + close socket on terminal |
| 4 | **High** | FE | No app-wide 404/catch-all route → unknown URL renders a blank shell pane | `frontend/src/routes/router.tsx` |
| 5 | **Med** | BE | Hung chat `BackgroundTask` blocks uvicorn graceful shutdown (>90 s, required container restart) | Move answer runtime off `BackgroundTasks` / bound shutdown |
| 6 | **Med** | FE | a11y: no `aria-live` for streaming answer; no per-code-block copy; **no Tab focus-trap** in any `aria-modal` dialog; no skip-to-content link; no managed focus/scroll-reset on route change | FE |
| 7 | **Med** | FE | Autoscroll never yields (scroll handler on a non-scrolling element) → yanks user to bottom on every token | `frontend/.../ChatThread.tsx` |
| 8 | **Med** | FE | Source "Sync now" failures silently swallowed; document-content fetches bypass the 401 refresh-retry; frozen typeahead/recent/saved-search contract unimplemented in UI | FE |
| 9 | **Low** | BE | `ruff format` would reformat 1 file; logout doesn't invalidate the already-issued (short-lived) access token; WS denial surfaces as HTTP 403 not a 1008 close; `sync_source` reports `ready` with `indexed_count=0` when all docs failed; served `info.version` stale at `0.0.1` | — |
| 10 | **Low** | FE | Markdown re-parses every token (O(n²)); native `window.confirm` for doc/collection delete (inconsistent); access token in WS URL query string; wire types hand-authored (generated client absent); `retry:1` no backoff | — |

---

## 6. Caveats

- **`OPENROUTER_API_KEY` is blank in the running stack.** This is *why* `GET /search` 503s, new-upload/sync ingestion fails at the embed step, and chat answers can't generate. Everything degrades cleanly (typed 503 / terminal `failed`, no leaks); pre-seeded docs already have embeddings. Setting the key is required to exercise the LLM-dependent happy paths and to fully validate the §4 fix.
- **Code-version split.** The automated suites ran against the worktree branch; the live passes ran against the deployed `main` image (ahead of the branch — it serves `/preferences`, `/saved-searches`, `/search/suggest|recent` routes absent from the branch). Both are clean modulo the above.
- Responsive layout was verified via CSS + a passing narrow-width component test; a live narrow-width screenshot could not be captured (browser-control tooling did not honor the viewport resize).

## 7. Recommended next steps

1. **Fix #1 first** — it disables the core product flow. Add bounded replay to `RedisBackplane` (or switch to Redis Streams) **and** a first-token watchdog in `useChatStream.ts` that synthesizes a terminal error with retry.
2. **Fix #2** — run the app under a dedicated non-superuser, non-`BYPASSRLS` role so the documented defense-in-depth RLS backstop is actually active.
3. Triage #3–#8 into the issue pipeline; #9–#10 are low-cost cleanups.
4. Set `OPENROUTER_API_KEY` in the stack and re-run the LLM-dependent E2E (grounded answer with citations, search results, ingestion to `ready`).
