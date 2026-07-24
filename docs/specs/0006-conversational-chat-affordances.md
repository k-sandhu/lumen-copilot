# Spec 0006 — Conversational chat affordances

> Status: **proposed** (Phase 0 of feature [#429](https://github.com/k-sandhu/lumen-copilot/issues/429)).
> Contract-first (ADR-0006): this spec and the matching `contracts/` additions are
> frozen before the backend/frontend feature code. Grounds itself in the mission
> filters ([AGENTS.md §2](../../AGENTS.md)), the security & domain invariants
> ([spec 0004](0004-security-and-domain-invariants.md)), the agent runtime
> ([ADR-0011](../architecture/0011-assistant-and-agent-runtime.md)) and the
> envelope semantics fixed by [ADR-0016](../architecture/0016-context-engine-and-cache-first-prompting.md).

## 1. Why

Five product asks (sponsor, 2026-07-17), all on one surface — the chat turn and
its composer — that together turn a static Q&A into a guided conversation:

1. **Clarifying questions** — instead of guessing at an ambiguous request, the
   model can ask the user a structured question with clickable options
   (Claude-Code-style `ask_user`).
2. **Live progress steps** — while the model works, the user sees which stage the
   run is on (preparing → thinking → answering → finalizing), not a blank pane.
   (Today *nothing* streams until a whole turn completes — turns are buffered.)
3. **Suggested follow-up questions** — after an answer, up to 3 clickable
   follow-up chips.
4. **Composer prefill** — the best next question ghost-fills the empty composer
   (accept with Tab/click; typing dismisses).
5. **Composer history** — ArrowUp/ArrowDown recalls the user's own previous
   messages bash-style, stashing/restoring the in-progress draft.

## 2. Decisions (and why)

- **`ask_user` ends the turn — no suspend/resume.** The chat WS is strictly
  one-directional (producer → client; `realtime/chat_ws.py` never reads client
  frames) and the runtime holds no conversational locks. So the tool does **not**
  block mid-run awaiting input: when the model calls it, the runtime persists the
  question as the turn's assistant message, emits one `event:ask_user`, and ends
  the stream with `done(finishReason="ask_user")`. The user's choice arrives as a
  **normal next user message** (`POST /chat/sessions/{id}/messages`); the next
  turn reads the question + reply from ordinary history. No new inbound channel,
  no runtime state machine, no timeout semantics.
- **`ask_user` is a registered, governed tool** (`services/tools/impls/ask_user.py`:
  T0, read-only, `default_offered`) so allow-listing/assistant `tool_allowlist`
  govern it like any tool — but the chat runtime **intercepts** it before the
  `ToolRunner` (it is a control-flow signal, not an executable). Executed through
  the runner anyway (sub-agent/headless contexts), its handler returns a typed
  `ok=false` "interactive-only" result — a background run can never block on it
  (ADR-0015 runs escalate instead).
- **First valid `ask_user` wins; the rest of that batch is dropped.** If a turn
  mixes `ask_user` with other calls, the question ends the turn and the other
  calls are not executed (nothing dangles: the in-run transcript is discarded at
  turn end, and persisted history never contains raw tool protocol).
- **Malformed `ask_user` arguments never surface.** Invalid args (empty/1/>4
  options after trimming & dedupe, empty question, over-long text) become a typed
  `ok=false` tool *result* the model reads, and the loop continues — the user
  eventually gets a normal answer (the forced-synthesis backstop already
  guarantees one). A broken question is a model error to recover from, not UI.
- **The question persists; steps and suggestions do not.** The question + options
  are conversational state (must survive reload to stay answerable) → nullable
  JSONB `question` on `messages` + additive `Message.question` on REST. Steps and
  suggestions are run-visibility state, same posture as `event:narration`
  (ADR-0016 §6): never persisted, replayed idempotently by `seq`, absent after
  reload.
- **Steps are a backend-owned vocabulary, dumb frontend.** The runtime emits
  `event:step {key, label, state}` at phase boundaries; the UI renders whatever
  arrives (spinner → check), so later stages (sub-agent progress ADR-0018 §5,
  memory) slot in without FE changes. Steps do **not** duplicate per-tool detail —
  `tool_call`/`tool_result` events already carry that; the stepper brackets them.
  This is deliberately distinct from (and coexists with) #414's `event:narration`
  (model narration *text*), which stays its own issue.
- **Suggestions are one cheap post-answer LLM call on the session's resolved
  route**, generated from the visible conversation only (question + final answer
  tail) — **no retrieval**, so no new INV-2 surface: nothing can be suggested
  from content the caller couldn't already see. Bounded by a timeout; any
  failure/parse miss ⇒ no event, never an error (an answer must never degrade
  because a nicety failed). Skipped entirely when the turn ended in `ask_user`
  (the options *are* the suggestions). Config-gated
  (`CHAT_SUGGESTIONS_ENABLED`, default on; count + timeout also config). The
  call's token usage is recorded on its **own** `llm_usage` row attributed to the
  **actual** model that produced it (ADR-0016 §2.6 actual-model attribution) —
  because a dedicated suggestions model (#490) is a *different* model from the
  answer's, its spend must land on that model's row, not the answer model's — while
  staying linked to the same session/answer so it is accounted (#409) and
  queryable. The spend is recorded whether or not the output parses (the tokens
  were billed regardless). Also skipped for the honest "couldn't find
  it" fallback answer (suppress suggestions on refusal/low-confidence answers —
  HAX guideline 10), and the client promotes chips/ghost only after a successful
  terminal `done`.
  - **Off the critical path (#489).** Suggestions no longer sit *before* `done` on
    the answer's latency path. The terminal `done` is published FIRST — declaring
    `pendingSuggestions=true` when the nicety will be attempted — so the UI settles
    the instant the answer is complete; the suggestions are then generated and
    delivered as a **post-terminal** `event:suggestions` within a bounded server
    grace window (`CHAT_SUGGESTIONS_GRACE_SECONDS`, the suggestions timeout + a
    margin). `done.usage` reports the ANSWER only, while the suggestions cost is
    accounted on its own `llm_usage` row (attributed to the actual suggestions model,
    #490/ADR-0016 §2.6; #409). A failure/timeout produces no event
    and cannot touch the already-published terminal; there is still exactly one
    terminal, and it is still `done`.
- **Prefill is a ghost, never a write.** The top suggestion renders as ghost text
  in the *empty* composer with an explicit accept affordance (Tab or click);
  typing anything dismisses it. The composer's draft is never overwritten —
  prefill cannot race or clobber user input by construction.
- **Composer history is client-side, current conversation only.** ArrowUp (caret
  on first line) walks the caller's own prior user turns newest-first from the
  already-loaded message list; ArrowDown walks back and finally restores the
  stashed draft. No server state, no cross-session recall (a server-side history
  surface already exists for search — spec 0005 — and is deliberately not
  extended here).

## 3. Contract surface (frozen shapes)

### 3.1 WS envelope additions (`contracts/websocket-envelopes.schema.json`) — all additive

New `event.name` values riding the existing chat stream:

| Event | Payload (`$defs`) | Cardinality / ordering |
|---|---|---|
| `event:step` | `ChatStep { key, label, state: started\|completed, detail?, turn? }` | many; phases in run order; a re-`started` key restarts that row (e.g. `think` per turn) |
| `event:ask_user` | `ChatAskUser { messageId, question, options[{label, description?}], allowFreeText }` | ≤ 1 per stream; if present the stream has no suggestions and ends `done(finishReason="ask_user")` |
| `event:suggestions` | `ChatSuggestions { messageId, suggestions: string[1..N] }` | ≤ 1 per stream; POST-terminal (#489) — after `done(pendingSuggestions=true)`, within a bounded grace window |
| `event:answer_retract` | *(no data)* | speculative-streaming retraction (#488); discards every answer `delta` streamed so far for the currently-streaming turn — targets at most one un-finalised speculative block, never appears after the answer is finalised nor after a terminal |

The answer surface now streams **speculatively** (#488, ADR-0016 §6): the answer
turn's text streams live as `delta` *before* the turn is proven tool-free. If the
turn then reveals a tool call, one `event:answer_retract` discards those
speculative deltas — the same text is re-emitted as `event:narration` (the #414
transient affordance), so no content is lost, only its classification
(answer→narration) is corrected. Clients clear the live answer text on
`answer_retract` and ignore it once the answer has settled.

The full lifecycle (authoritative in `contracts/websocket-envelopes.schema.json`
`x-chatStream.lifecycle`): `start → ( delta | event:answer_retract | event:step |
event:narration | event:tool_call | event:tool_result | event:citation |
event:code_output | event:code_result | event:ask_user )* → done | error → [
event:suggestions ]?` — exactly one terminal (`done`/`error`), with the single
narrow post-terminal exception being one `event:suggestions` after a
`done(pendingSuggestions=true)`.

Step keys fixed by this spec: `prepare` (context assembly), `think` (one model
turn; `turn` ordinal, `detail` set on completion), `finalize` (persist +
citations), `suggest` (follow-up generation). Additive keys are allowed later;
clients must render unknown keys generically (label is server-supplied) and — per
the existing reducer rule — ignore unknown event names entirely.

`ChatDoneData.finishReason` gains the value `ask_user` (free-string field;
description updated).

### 3.2 REST additions (`contracts/openapi.yaml`) — additive

`Message` gains an optional `question` object (assistant turns that asked one):

```yaml
AskUserQuestion:
  type: object
  additionalProperties: false
  required: [question, options, allow_free_text]
  properties:
    question: { type: string }
    options:
      type: array
      minItems: 2
      maxItems: 4
      items:
        type: object
        additionalProperties: false
        required: [label]
        properties:
          label: { type: string }
          description: { type: string }
    allow_free_text: { type: boolean }
```

No new endpoint: the answer to a question is an ordinary `SendMessageRequest`.
"Answered" is client-derived (the question message is no longer the last message)
— no stored flag, nothing to migrate later.

### 3.3 Tool schema (advertised to the model)

`ask_user(question: string, options: [{label, description?}] (2–4), allow_free_text?: bool=true)` —
description carries the usage guidance ("ask only when the request is genuinely
ambiguous and the answer materially changes the response; options must be
mutually exclusive; at most one question per turn"). The system prompt is
deliberately **unchanged** (prompt-cache stability, ADR-0016 §2).

## 4. Data model

One additive, reversible migration (0033): `messages.question` — nullable JSONB,
no index (read only via its message), no RLS change (the `messages` policies
already cover the row). Existing rows are untouched (`NULL` = no question).

## 5. Security & domain invariants (spec 0004)

- **INV-1/INV-2.** No new retrieval or cross-tenant surface: steps carry only
  runtime phase labels; suggestions derive only from the caller's own visible
  conversation; the question payload is authored by the model inside the caller's
  own stream and persists on the caller's own message row (tenant-scoped +
  RLS-backstopped like every message). The WS events ride the existing
  owner-bound stream (`bind_owner` unchanged).
- **INV-3.** A question turn makes no claims: it persists with **zero citations**
  (passages retrieved before the model chose to ask are *not* attached), and the
  citation chokepoint is untouched for answer turns.
- **INV-6.** The question turn still emits `answer.generated` (citation_count=0)
  and its preceding tool calls audit normally; the suggestions call adds **no**
  audit event (it is not a retrieval and produces no user-visible claim — same
  posture as the model's own narration) but **is** cost-accounted in `llm_usage`.
- **INV-8.** Malformed model output (bad `ask_user` args, unparseable
  suggestions) is contained server-side (typed tool error / silent skip) — never
  a 5xx, never a broken stream.

## 6. Acceptance criteria

The feature ACs live on [#429](https://github.com/k-sandhu/lumen-copilot/issues/429)
(AC-1..AC-5, AC-N1..AC-N3) and bind to this spec's shapes; the load-bearing
negatives: malformed `ask_user` → recovered normal answer; suggestions
failure/disabled → silent skip; replay duplicates nothing and re-arms no answered
question; no new INV-2 surface.

## 7. Frontend behavior

- **Stepper**: renders `step` events in arrival order (running = pulse, done =
  check) above the streaming turn; collapses once answer text flows; the
  `suggest`/`finalize` steps also stop the caret (the answer text is settled).
  Streaming, error, and disconnect states all leave no orphaned spinner.
- **Question options**: buttons on the assistant turn (live from `event:ask_user`,
  reloaded from `Message.question`). Active **only** while that message is the
  conversation's last and no stream is in flight; clicking sends the option label
  as a user message (visible, honest transcript); `allowFreeText` keeps the
  composer usable as the "other" path. Answered/older questions render inert with
  the chosen option highlighted (derived from the following user turn).
- **Suggestion chips**: under the latest settled assistant turn only; click =
  send that text. Gone on reload (ephemeral by design).
- **Ghost prefill + history**: per §2. All interactive elements keyboard-reachable
  with visible focus and ARIA labels; `aria-live` politeness on step updates;
  honors `prefers-reduced-motion` (frontend/AGENTS.md quality bar).

## 8. Non-goals

- No mid-run blocking questions, multi-question forms, or answer-by-WS (the
  socket stays one-directional).
- No suggestion personalization/learning; no persistence of suggestions.
- No server-side composer history; no cross-session recall.
- `event:narration` (#414), sub-agent progress envelopes (ADR-0018 §5), and any
  workflow *engine* (ADR-0011 §6) remain their own work.

## 9. Verification

- **Contract:** schema JSON parses; openapi additions are additive
  (`pnpm gen:api` builds; `api/types.ts` reconciled).
- **Backend:** `ruff` + `mypy --strict` + `pytest` green incl.: step order;
  ask_user happy/malformed/mixed-batch; question persisted + zero citations +
  `finishReason=ask_user`; suggestions emitted/disabled/timeout/parse-fail/skip
  after ask_user; the suggestion spend is recorded on its own actual-model
  `llm_usage` row (accounted even on a parse miss, idempotent); migration up/down.
- **Frontend:** Vitest state coverage (streaming/done/error/replay) for reducer +
  stepper + options + chips + composer (ghost accept/dismiss; ArrowUp/Down incl.
  draft stash/restore, multiline guards); `tsc`/ESLint clean.
- **Live:** compose stack round-trip — ambiguous ask → options → click → grounded
  answer → suggestions → chip → answer (verified before merge).

---
*Provenance: product ask by the sponsor in-session (2026-07-17), scoped to the
existing runtime per ADR-0011 ("configured single-agent chat") and the envelope
rules of ADR-0016. Feature issue: [#429](https://github.com/k-sandhu/lumen-copilot/issues/429).*
