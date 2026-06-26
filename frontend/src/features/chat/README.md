# Chat — streaming, citations, model picker & history (`features/chat`)

The frontend chat slice (issue #50), built on the auth foundation (#48) against the
**frozen** contract (`contracts/openapi.yaml` 0.1.0 §chat/§models and
`contracts/websocket-envelopes.schema.json` chat payloads). It builds in parallel
with the chat runtime (ADR-0006) — conform to the contract, mock the WS stream in
dev and tests.

## Flow

1. **Send** a user message → `POST /chat/sessions/{id}/messages` returns the
   persisted user message + a `stream_id`.
2. **Subscribe** to that `stream_id` over the WS envelope at `/chat/<streamId>`:
   `start` → ( `delta`(token) | `event:tool_call` | `event:tool_result` |
   `event:citation` )\* → `done` | `error`.
3. **Render live**: delta tokens stream into the assistant bubble; tool activity
   shows "searching documents…"; each `event:citation` becomes a clickable
   passage-level reference.
4. **Persist**: on `done` the messages query is refetched; the live turn is
   retired only once the persisted assistant message (`done.messageId`) appears in
   the reloaded `GET .../messages` — so the answer never flickers out.

## Where the pieces live

Transport (the `api/` boundary — the only backend caller):

- [`api/chat.ts`](../../api/chat.ts) — typed session CRUD, `listMessages`,
  `sendMessage`.
- [`api/models.ts`](../../api/models.ts) — `listModels` (the picker registry).
- [`api/documents.ts`](../../api/documents.ts) — `fetchDocumentContent` for
  citation click-through: fetches the original bytes with the bearer (INV-4 —
  `GET /documents/{id}/content` is bearer-authed, not cookie-authed) and hands
  back a `blob:` URL the viewer renders.
- [`api/ws.ts`](../../api/ws.ts) — the typed WS client (reused from #48); features
  consume a hook, never the raw socket.

Feature (`features/chat`):

- `model/streamReducer.ts` — **pure** reducer folding the WS envelopes into UI
  state (text, citations, tool activity, terminal). Dedupes by `seq`. Unit-tested
  in isolation including terminal `error` and synthetic disconnect.
- `model/useChatStream.ts` — subscribes to one stream via the WS client and the
  reducer; an unexpected disconnect or first-token/idle watchdog expiry becomes
  a terminal error with retry. Clean terminal envelopes close the socket so
  completed streams do not reconnect. Cancellable. The client factory and
  watchdog deadline are injectable for tests.
- `model/queries.ts` — TanStack Query hooks: sessions, messages, models, and the
  new/rename/delete/send mutations. Server data is **never** mirrored into Zustand.
- `model/chatStore.ts` — Zustand UI-only state: active session, in-flight stream
  id, per-turn model, citation viewer target (incl. the source freshness label).
- `model/presentation.ts` — **pure** trust-signal derivation for the #89 re-skin:
  the retrieval-trace summary/steps (`buildRetrievalSummary`), relative-time +
  staleness, the model-badge label, the SourceInspector passage, and the
  source-inspector metadata rows (`sourceMetadataRows`, #120). No I/O.
- `components/` — `ChatView` (orchestrator), `ChatThread`, `MessageBubble`,
  `Composer`, `ModelPicker`, `HistorySidebar`, `KnowledgeModeChips`,
  `ToolActivity`, `DocumentViewer`, `AnswerFooter`.

## Trust-signal re-skin (#89, ADR-0007 §1 / DESIGN.md §1/§6)

The screens consume the design-system kit (`@/ui`) — **no contract change**, every
signal derived from data the turn already has:

- **Inline citation chips → SourceInspector:** the kit `CitationChip` opens the
  cited document; the viewer leads with the kit `SourceInspector` (cited passage
  `<mark>`-highlighted + freshness).
- **RetrievalTrace:** a collapsible "Looked at N sources · M passages · K
  excluded" built from the distinct cited sources, the retrieval-tool hit counts,
  and any excluded count — excluded candidates render muted (mission filter #4).
- **FreshnessPill** on cited sources; a **model badge** on assistant answers
  (friendly label from `GET /models`, falling back to the model id).
- **Knowledge-mode chips** on the composer surface the grounding scope; "Company
  sources" (all permitted docs — the existing behavior) is the default. Modes the
  contract has no backing for (web, model-only) are **not** offered — no invented
  product behavior (AGENTS.md §8).

## Answer footer + inspector metadata (#120, wireframe `chat.html`)

A polish pass that adds the wireframe's answer-bubble footer and the
source-inspector metadata grid — **content only** (the screen nests in the app
shell via `PageChrome`; no top bar / RouteGuard added here). Like #89, **no
contract change** and **no invented data**:

- **`AnswerFooter`** (under a settled assistant answer): a **"Permission-checked"**
  status shown only when the answer is grounded in ≥1 cited source (honest by
  construction — the backend returns only sources the caller may see, INV-2/INV-3,
  so a grounded answer is permission-checked; never a bare claim), an **answer
  time** ("answered <ago>") derived from the real message timestamp, plus three
  actions: **helpful / not-helpful** and **copy**. The footer is omitted while the
  turn is still streaming. The timestamp is labelled as the **answer** time — when
  the answer was produced — and **never** as source "freshness"/"last indexed":
  the chat wire carries no source-provenance timestamp, so presenting the answer
  time as source recency would fabricate provenance (GUARD #120).
- **Helpful / not-helpful are LOCAL-ONLY UI** — a mutually-exclusive,
  clearable `aria-pressed` toggle. There is **no backend feedback endpoint**, so
  the vote persists **nothing** and the UI never implies it does (honest per #120).
- **Copy** is **client-side only** (`navigator.clipboard.writeText`) — it copies
  the rendered answer text and flips to a transient "Copied" confirmation.
- **Source-inspector metadata grid** (in `DocumentViewer`): the wireframe's
  **owner / last-modified / last-indexed** rows. The chat/citation wire
  (`Citation` / `ChatCitation`) carries **none** of these — and the only timestamp
  a chat turn has is the **answer/message time**, which is the answer's age, *not*
  when the source was indexed or modified (that source-indexing metadata lives on
  the separate search-result contract, not the chat citation wire). So every row —
  owner, last-modified, **and last-indexed** — renders **"Not available"** unless
  a source actually carries a real value; the answer time is never presented as
  source provenance (GUARD #120: never fabricate where a doc was last indexed).
  Each row lights up honestly only if a source ever starts carrying that field.

## Enterprise re-skin (#136, wireframe `chat.html` / DESIGN.md §1/§6)

The chat — the product's hero surface — is brought to the same fidelity as the app
shell (#110): the canonical layout in `docs/wireframes/chat.html` ported onto the
**production** token system in a co-located, token-driven stylesheet
[`chat.css`](../chat.css) (imported by `ChatView`). No wireframe CSS is copied
verbatim; like the shell, every size is `calc(px * var(--fs|--space))` and every
color is a `--token`, so the chat re-skins under any `[data-theme]`/`[data-mode]`
and moves live with the density axis. Class names are `lc-` prefixed.

- **Conversation** is a centered ~840px reading column with an 8px-rhythm — each
  turn is an **avatar + body row** with a "You"/"Lumen" name (replacing the old
  right-aligned bubbles). The user turn is a soft accent card; the assistant answer
  sits flush and reads a touch larger (`.lc-answer`).
- **Composer** is an elevated rounded card with a focus glow, the knowledge-mode
  chips above it, and a tidy bar (model picker + round send / Stop). The send/Stop
  affordance preserves the cancellable-stream behavior and accessible names.
- **History sidebar** (`HistorySidebar`) gains a **New chat** primary action, a
  client-side **search** filter (honest "no match" state), sessions **grouped by
  recency** (Today / Yesterday / Previous 7 days / Older), and a one-line meta
  (`sessionMeta`) built **only** from fields the session wire carries
  (`message_count`, `model`, `updated_at`) — never a fabricated "sources" count.
  `groupSessionsByDay` / `dayBucket` / `sessionMeta` are pure and unit-tested.
- **Sources-used strip** renders each cited source as a numbered row (kit
  `CitationChip` → opens the inspector) with a `FreshnessPill` and a
  `PermissionPill` ("You have access" — honest by construction, INV-2).
- A calm **empty/no-session hero** replaces the bare prompt.

The re-skin is **presentation-only** — no contract change, no new transport, and
no invented affordance (no attach/upload/web control that does nothing). All tested
behavior and accessible names are preserved; new pure helpers carry their own tests.

## Invariants honored

- **INV-3 Citation integrity** (spec 0004): citations render only from the
  passages the stream/server actually returned; a zero-citation answer is shown
  honestly ("No sources were cited for this answer") — the UI never fabricates a
  reference.
- **INV-1/INV-2** (tenancy/permission): a cross-tenant or not-permitted session /
  message / document returns 404; the UI surfaces it as an actionable error
  (retry), never a blank pane. Covered in `api/chat.test.ts` and
  `HistorySidebar.test.tsx`.
- **INV-8** (malformed input): a 422 from send surfaces as a typed error in the
  composer.

## State coverage (frontend/AGENTS.md quality bar)

Every async surface implements loading / empty / error+retry / populated /
streaming. The streaming UX autoscrolls but yields to the user, and the stream is
cancellable (Stop button + on navigation/disconnect). Markdown is rendered through
the sanitizing pipeline — never raw, never `dangerouslySetInnerHTML`.

## At BE wire-up (#24 / #26)

Contract-true today against mocks. At integration, confirm: the send response's
`stream_id`, the WS path the backend expects for subscription, and that
`event:citation` payloads carry `documentId` + `charStart/charEnd` resolving to a
permitted document.
