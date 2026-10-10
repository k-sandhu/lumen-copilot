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
