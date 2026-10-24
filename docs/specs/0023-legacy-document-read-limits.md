# Legacy document read limits

Tracking: [#610](https://github.com/k-sandhu/lumen-copilot/issues/610), [#758](https://github.com/k-sandhu/lumen-copilot/issues/758).

`get_document` reads a permitted document's prefix, at most 2,400 characters;
the context engine may lower this to four times its snippet allowance. It reports
the zero-based, end-exclusive returned range and total extracted-text length.
If more text exists it explicitly marks the missing range. Recovery advice uses
the effective allowed catalog supplied by the governed runner, including isolated
call scopes. When `search_text` is available it recommends a targeted query.
Otherwise it states that this assistant cannot inspect the omitted text and asks
the user to supply the relevant excerpt or switch to an authorized search-capable
assistant. It never widens an allowlist or rewrites a published version. A handler
without an authoritative catalog uses the same user-assisted fallback.
Repeating this legacy read cannot advance its range.
It must not describe the prefix as the full document or imply omitted text is empty.

Short reads report their exact range without a truncation marker. The closed
legacy schema remains one required string, `document_id`. Resolution proceeds
in this order (#758, integrated with the conversation handles in spec 0025):

1. With an active `ctx.handles`, a trimmed value matching the conversation handle
   grammar (`[SDW][1-9][0-9]*`) resolves only through that registry. A valid D
   identity uses the permissioned document read. Unknown/forged handles,
   non-document handles and invalid document entries perform no retrieval I/O;
   they never fall through to a legacy resolver or filename lookup.
2. Otherwise, a configured `document_id_resolver` gets the trimmed value. A
   returned UUID uses the permissioned document read directly, without chunk or
   filename fallback, even if that read is not found.
3. Otherwise, `RetrievalService.resolve_document_text` resolves a document UUID,
   then a chunk UUID to its permitted containing document, then a literal exact
   filename, then the stripped-name fallback. Every lookup stays in `retrieval/`
   under its shared tenant + owner-or-grant predicate.

Legacy `search_text` passage headers carry the document id (`document id: …`);
canonical rendering is unchanged. After unambiguous UUID extraction, the trimmed, unquoted
literal name is matched first; prefixes and trailing parentheticals are stripped
only if it has no permitted matches. Literal matches, including ambiguity,
are never merged with fallback matches. Several permitted documents with that
exact name return the permitted candidates so the caller can retry with one id.
Missing, malformed, forged and forbidden inputs have the same corrective
`tool_bad_args` response, including identical content, `ok`, `error`,
`document_ids`, `payload` and `hit_count` within the same context (INV-2).
They disclose no length or range. Recovery names only allowed tools and values
they actually show: `search_text` document/chunk ids, `search_documents` or
`list_documents` ids, and `find_documents` D handles (or UUIDs without a handle
registry). It does not suggest `search_passages` S handles or display titles as
legacy read arguments. The accepted-shapes text mentions conversation D handles
only when a registry or resolver is configured.
Payload metadata (`returned_range`, `total_length`, `truncated`) is internal tool
output, not a new REST or WebSocket envelope. Range navigation is separate work.

Verification: synthetic regression tests in `test_tools_retrieval.py` cover a
3,000-character body, tight context allowance, a short body and forbidden reads,
including model-visible range/total assertions. Published prefix-only and
search-capable assistant regressions exercise the governed runner in serial and
isolated scopes, follow the offered recovery, and preserve the original snapshot.
