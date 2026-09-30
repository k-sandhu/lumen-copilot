# Legacy document read limits

Tracking: [#610](https://github.com/k-sandhu/lumen-copilot/issues/610).

`get_document` reads a permitted document's prefix, at most 2,400 characters;
the context engine may lower this to four times its snippet allowance. It reports
the zero-based, end-exclusive returned range and total extracted-text length.
If more text exists it explicitly marks the missing range and recommends a
targeted `search_text` query. Repeating this legacy read cannot advance its range.
It must not describe the prefix as the full document or imply omitted text is empty.

Short reads report their exact range without a truncation marker. Missing and
forbidden documents have the same response and disclose no length or range.
Payload metadata (`returned_range`, `total_length`, `truncated`) is internal tool
output, not a new REST or WebSocket envelope. Range navigation is separate work.

Verification: synthetic regression tests in `test_tools_retrieval.py` cover a
3,000-character body, tight context allowance, a short body and forbidden reads.
