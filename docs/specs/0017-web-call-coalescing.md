# Public web-search calls within one answer

Tracking: #598. Related answer-citation selection: #436.

After governance, identical built-in T0 `web_search` calls in one answer share
one bounded execution. The key includes requester/tenant and all arguments with
only surrounding query whitespace normalized. Different options, answers,
corpus reads, dynamic MCP tools and write tools never share executions.
Successful empty results are reusable; failures are retried by a later call.
Each consumer retains its own call ID, result, audit events and invocation row.
Audit metadata identifies the original execution and deduplicated reuse without
storing query text. Reuse retains source payload provenance.

Cancellation releases a consumer, cancels and awaits the producer when no
consumer remains, clears the failed/cancelled entry and records the cancellation
before advancing the ordered persistence drain. Another active consumer may
finish the shared execution. Provider errors remain typed and honest; empty
provider results differ from malformed/unusable provider output. External live
smoke is opt-in; fixtures exercise normalization without network dependencies.

R2 acceptance criteria (review R1-001 / R1-002):

- A batch infrastructure failure aborts the ordered persistence coordinator and
  wakes its waiters before reaping workers. Cancelled peers never wait on a failed
  ordinal. The runtime emits one error terminal and rolls back the answer; a
  consumer cancelled in a surviving batch still records its own cancellation.
- An empty SearXNG HTTP-200 response with `unresponsive_engines` is a retryable
  failure, classified as rate limited, blocked, timeout, or unavailable. Inspect
  at most 64 engine entries and 256 characters per diagnostic; never expose raw
  diagnostics. Mixed failures use rate limited, blocked, then timeout precedence;
  unknown/malformed nonempty metadata falls back to unavailable. Useful partial
  results and genuine empty responses remain reusable.
