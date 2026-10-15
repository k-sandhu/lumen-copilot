# Legacy document read limits

Tracking: [#610](https://github.com/k-sandhu/lumen-copilot/issues/610).

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

Short reads report their exact range without a truncation marker. Missing and
forbidden documents have the same response and disclose no length or range.
Payload metadata (`returned_range`, `total_length`, `truncated`) is internal tool
output, not a new REST or WebSocket envelope. Range navigation is separate work.

Verification: synthetic regression tests in `test_tools_retrieval.py` cover a
3,000-character body, tight context allowance, a short body and forbidden reads,
including model-visible range/total assertions. Published prefix-only and
search-capable assistant regressions exercise the governed runner in serial and
isolated scopes, follow the offered recovery, and preserve the original snapshot.
