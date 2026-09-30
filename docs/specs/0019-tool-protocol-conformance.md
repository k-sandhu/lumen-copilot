# Versioned route-specific tool-protocol conformance

Tracking: #636; supplies the T14 qualification evidence requested by #597.

The suite runs independently per model and immutable route revision through the
existing LLM gateway. Synthetic tools read no enterprise, web or MCP data and
perform no writes. Four probes cover exact-once calls with strict-compatible
required nullable/enum fields and a maximum-length safe name, parallel calls,
correlated result use on the next turn, and explicit stream closure. Prose such
as TOOL_OK never substitutes for execution evidence. Parallel support is reported
separately; core qualification requires the other three probes.

Each probe permits at most two model turns, 256 output tokens per turn, bounded
event/text/call counts and a configurable deadline no greater than 60 seconds.
Only valid calls to the advertised synthetic tool receive unpredictable result
markers, absent from the user prompt. The final answer must use those markers
and make no further calls. This exercises provider tool-result round-trips,
including routes requiring additional opaque state; unsupported gateway routes
fail honestly rather than gain provider-specific workarounds.

Reports record model, route revision, suite version, UTC time and safe outcomes;
they contain no prompt, generated answer or provider exception text. Qualification
evidence expires after seven days and is invalid for a changed model/route/suite
or future timestamp. A report is evidence, not an uptime or answer-quality claim.
Provider probes require an explicit CLI opt-in; default tests use deterministic
fakes. Cancellation and timeouts close active streams. The suite adds no native
provider features (T15) or general answer-quality benchmark (T01).

Automatic picker/workload enforcement and production action-success detection
remain the broader #597 decisions. No model is claimed qualified until an actual
opt-in probe produces current passing evidence for its configured route.

From `backend/`, an operator can run an explicitly authorized probe with the
configured gateway and an immutable revision identifying that route:

```sh
uv run python -m app.services.tools.conformance --model provider/model \
  --route-revision reviewed-route-revision --timeout 30 \
  --output conformance-report.json --allow-provider-probes
```

Exit 0 indicates core protocol qualification; exit 1 means at least one core
probe failed. Inspect each case and the optional parallel result, then use
`is_current` against the actual model and route revision before treating the
report as current evidence. No configured model is automatically enabled by
this command. Provider calls can incur cost, so the opt-in is required.
