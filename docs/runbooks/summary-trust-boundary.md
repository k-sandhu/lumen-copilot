# Rolling-summary trust boundary (#573)

Provider output and rolling summaries are **untrusted data**. A model must
never be able to place internal control text (system/summarizer prompts,
the conversation-memory wrapper) or hidden reasoning where a user can see
it, where it is persisted, or where a later prompt would treat it as an
instruction.

## Where the boundary is enforced

| Hop | Enforcement |
|---|---|
| Provider stream → user/save | `backend/app/llm/reasoning_guard.py` strips inline `<think>`/`<thinking>`/`<reasoning>`/`<reflection>` blocks and chat-template control tokens inside `llm/gateway.py` (`chat`, `stream`, `stream_tools`), statefully across chunks, **before** publishing. What streams is what is saved. A separate vendor reasoning field is dropped by reading only `content`. |
| Summarizer input | `tasks/summarize.py` builds input only from canonical persisted messages, rendered as JSON inside `<new_turns_data>`; the previous summary rides inside `<previous_summary_data>` after the same validation as the read path. Assistant rows are reasoning-stripped; user rows stay verbatim. |
| Summarizer output → storage | Must parse as the strict typed JSON `{"facts", "decisions", "open_questions", "evidence_refs"}` (`backend/app/domain/summary.py`): exact keys, bounded string lists, no control tokens, reasoning tags, serialized envelopes, or internal prompt/wrapper phrases. Verbatim cited-source redaction (#446) runs per item, then the final canonical JSON is validated again before storage in the existing `Text` column. |
| Storage → prompt | `chat_service.send_message` validates on read; `llm/context.py` validates again and renders as fenced, escaped `summary-data` inside a **user-role** message. It never enters a system/developer message. |
| Recall (`read_conversation`) | Stored assistant turns are reasoning/control-stripped. Each turn is escaped inside a `turn-data` fence in the tool result; user-authored text stays available as data. |

## Failure behaviour

- A summarizer completion that fails validation is **rejected**: the previous
  known-good summary and its coverage cursor are kept, the covered turns stay
  verbatim, the spend is recorded in `llm_usage`, and a
  `session.summarized` audit event with `outcome=error` carries only a
  sanitized reason code (e.g. `invalid structured summary (unknown_keys)`).
  The rejected payload is never stored, logged, or put in audit metadata.
  The structured log event is `summarize.invalid_summary` with a `reason`
  code and optional character count, plus operational correlation identifiers.
- A stored summary that fails validation on read is **treated as absent**:
  no summary segment and no compaction cursor, so the turn falls back to the
  full verbatim history. Log events: `chat.summary_rejected_on_read` /
  `context.summary_rejected` (reason code only).
- Legacy free-text rows are invalid too: there is no prose compatibility
  bypass. The summarizer rebuilds from canonical history, replacing the invalid
  row only if its version still matches. A racing valid summary wins; evidence
  updates remain independent. A rejected rebuild leaves the row and cursor
  unchanged, and readers continue using full-history fallback.
- Inline reasoning blocked at the gateway is invisible by design; the only
  signal is the `llm.reasoning_blocked` log with a block **count**. Nested tags
  and split markers stay suppressed; unmatched close tags do not release a
  reasoning block. Exact internal prompt/wrapper sentinels suppress the rest of
  that provider completion. Buffers and nesting are bounded and fail closed.
  Non-streamed completions report whether filtering occurred so the summarizer
  rejects them rather than accepting a cleaned remnant as valid structured data.
- A length-capped tool-aware completion with an open reasoning block, a pending
  marker/sentinel prefix, or a suppressed remainder **stops at the safe partial
  answer**. The gateway emits a normal stop without flushing withheld text, so
  the runtime cannot resume hidden state through a fresh guard. Clean length
  completions still get the existing one continuation. This can conservatively
  shorten an answer ending in a possible marker or sentinel prefix.
- Impossible marker candidates are released immediately. Ordinary less-than
  comparisons, including operands beginning with `s` and long prose following
  an inequality, cannot consume the marker buffer or suppress the answer tail.
- If cited-source redaction expands an item past 500 characters or canonical
  JSON past 6,000 characters, final validation uses the same sanitized refusal
  path. No summary, version, or coverage cursor changes; spend remains recorded.

## Acceptance checks

- Only exact-schema JSON can advance coverage. Duplicate keys, role markers,
  nested envelopes, reasoning fields, and serialized data exceeding the storage
  cap are rejected, with no payload in logs or audit metadata.
- Both summary read boundaries accept the same canonical JSON. Summary content
  is fenced and escaped as user data; canonical input JSON escapes angle
  brackets so user text cannot close the summarizer's data delimiters.
- Every split of control/reasoning markers produces the same safe output as
  the one-shot guard. Ordinary text and user-authored prompt discussions survive.
- Length-continuation regressions compare published deltas with committed
  message read-back for split reasoning/control markers and internal sentinels;
  clean completions still continue. Comparison tests exhaust every chunk split.
- Redaction expansion at both item and storage caps preserves a prior valid
  summary, version and cursor, and cannot create an invalid first summary.
- Invalid rows fall back to full history and can rebuild without trusting the
  invalid cursor. Concurrent replacement checks the observed version.
- Tenant/owner isolation, permission redaction, audit rollback, and spend
  accounting keep their existing repository and service gates.

## What this is deliberately not

- Not a keyword filter on user content. User-authored text that merely
  discusses prompts (for example the phrase "Output ONLY") is stored
  verbatim, appears in history, and may be summarized as a fact. Only
  structural markers and this product's exact internal phrases are rejected,
  in generated summaries; the provider-output guard also blocks the exact
  internal control sentinels. User-authored rows are never keyword-filtered.
- Not content moderation and not a guarantee against fluent-but-wrong
  summaries: like the earlier quality gate, validation is cheap and
  deterministic; a plausible wrong fact still passes. Permission safety
  continues to rest on retrieval-time checks and mentioned-name redaction,
  not on summary prose.
- Untagged reasoning prose and arbitrary paraphrases of internal instructions
  cannot be identified reliably by this deterministic boundary. No semantic
  classifier is introduced. Exact-sentinel matches can conservatively suppress
  a legitimate provider explanation; the original user message remains stored.

## Operator checks

A route suddenly failing summarization shows up as repeated
`summarize.invalid_summary` warnings plus `session.summarized` error audits
with the same reason code, while chats keep answering from verbatim history
(at higher token cost). `not_json` after a model swap usually means the new
route cannot follow the JSON contract — pin a compliant
`CHAT_SUMMARY_MODEL`. `internal_prompt_text` / `reasoning_tag` means the
route is leaking control text and should be investigated as a provider
incident, not worked around by loosening validation.
