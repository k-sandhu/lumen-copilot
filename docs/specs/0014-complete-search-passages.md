# Complete search passages

Tracking: [#611](https://github.com/k-sandhu/lumen-copilot/issues/611).

`search_text` returns the complete text of each permitted retrieved passage,
with outer whitespace removed for display. A smaller legacy snippet allowance
does not cut passage text. The same rendering helper supplies compaction's
verbatim protected evidence, so the visible passage and its retained form agree.

The existing effective `k` ceiling (maximum 20; lowered by the context engine)
bounds result count. Context fitting runs before every model call and may return
the existing typed `context_too_large` error rather than exceed the model window.
Legacy document prefix reads retain their separate limit. Ranking, ingestion and
citation selection are unchanged by this fix.

Verification: a synthetic late-passage fact remains visible with 600- and
300-character legacy allowances; count-clamping and context-refusal tests remain
required. Record token/latency effects beside fact recall before merge.
