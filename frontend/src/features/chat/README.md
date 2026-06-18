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
- [`api/documents.ts`](../../api/documents.ts) — `documentContentUrl` for citation
  click-through to the original bytes.
- [`api/ws.ts`](../../api/ws.ts) — the typed WS client (reused from #48); features
  consume a hook, never the raw socket.

Feature (`features/chat`):

- `model/streamReducer.ts` — **pure** reducer folding the WS envelopes into UI
  state (text, citations, tool activity, terminal). Dedupes by `seq`. Unit-tested
  in isolation including terminal `error` and synthetic disconnect.
- `model/useChatStream.ts` — subscribes to one stream via the WS client and the
  reducer; an unexpected disconnect (socket closes with no terminal) becomes a
  terminal error with retry. Cancellable. The client factory is injectable for
  tests.
- `model/queries.ts` — TanStack Query hooks: sessions, messages, models, and the
  new/rename/delete/send mutations. Server data is **never** mirrored into Zustand.
- `model/chatStore.ts` — Zustand UI-only state: active session, in-flight stream
  id, per-turn model, citation viewer target.
- `components/` — `ChatView` (orchestrator), `ChatThread`, `MessageBubble`,
  `Composer`, `ModelPicker`, `HistorySidebar`, `CitationRef`, `ToolActivity`,
  `DocumentViewer`.

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
