# Classification diagnostics

Run from `backend/` (offline, no datastore or provider sockets):

```powershell
uv run --no-sync --extra dev python -m tests.eval.classification.harness --bins 10 --output C:/Temp/classification-report.json
uv run --no-sync --extra dev python -m tests.eval.classification.harness --bins 10 --baseline C:/Temp/classification-report.json --tolerance 0 --output C:/Temp/classification-repeat.json
```

The explicit zero tolerance above verifies repeatability of scripted diagnostics;
it is not an approved release tolerance. See spec 0031 for all metric definitions,
denominators, calibration support and owner gates. `synthetic.json` contains four
authored examples, bounded features produced by the Rust facade, and complete
scripted response recordings. It is not a recording of live model quality.
Primary and fallback comparisons bypass literal rules to isolate their behavior;
the rules-only mode reports abstentions as failures and known zero inference cost.
Fixture latency is simulated (rules zero means no inference latency); CPU replay
timing is separate. Live reports measure wall-clock cascade latency.

Private recorded labelled sets use the same JSON structure and stay outside this
repository; pass an external path with `--data`. For release calibration, use
`source: authorized_private`, disjoint `calibration` and `held_out` example IDs,
and `--approval` pointing to a reviewed configuration containing `approved_by`,
`approved_at`, `corpus_sha256`, `bins`, `top_level_tolerance`,
`review_error_target`, `minimum_support`, `confidence_level`. These are required,
with no release defaults. Unsupported groups retain null thresholds. Artifacts
do not alter the production classifier's review-required policy. Keep private
reports outside the public checkout too.

Optional live validation is one bundled synthetic invoice only:

```powershell
uv run --no-sync --extra dev python -m tests.eval.classification.harness --live --bins 10 --output C:/Temp/classification-live.json
```

Configure `OPENROUTER_API_KEY`, `CLASSIFICATION_TOKENIZER_PATH`,
`CLASSIFICATION_TOKENIZER_SHA256`, and `CLASSIFICATION_TOKENIZER_MODEL` through
the user environment. The tokenizer model must be `openai/gpt-6-luna-decisions`.
Missing configuration fails closed. Maximum three calls, no retries/fallback,
and conservative per-attempt ceilings of $0.016 under a $0.05 held-spend budget.
Do not repeat live mode within a session whose authorized call budget is exhausted.
Sanitized fixtures reconstruct validated choices/predicates/accounting; request
headers, credentials, arbitrary provider fields and raw errors are never captured.
No live run was made for this implementation.
