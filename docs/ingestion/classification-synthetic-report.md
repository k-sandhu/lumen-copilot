# Synthetic classification diagnostic — #694

Four authored examples and scripted responses validate the offline harness. These are not live provider quality measurements. Taxonomy 1.0.0; classifier classification-1.1; rules 1; explicit diagnostic bin count 10. Production calibration remains unapproved.

| Mode | Root / family / type accuracy | Facet macro F1 | Path ECE | Order flip rate | Mean / p95 latency (simulated ms) | Known cost / total (scripted USD) |
|---|---|---|---|---|---|---|
| rules | 25% / 25% / 25% | 0.000 | 0.000 | 0% | 0 / 0 | 0 / 0 |
| decisions | 100% / 100% / 100% | 1.000 | 0.291 | 25% | 45 / 45 | 0.00011 / 0.00011 |
| structured_output | 100% / 100% / 100% | 1.000 | 0.521 | 0% | 90 / 90 | 0.00011 / unknown |

The fallback has 11 unknown primary-attempt costs across four documents; known successful fallback costs remain visible. Rules-only abstentions remain in every accuracy denominator. Zero rules latency denotes no simulated inference latency; replay CPU time is separate in the JSON report. One ambiguous example changes path under the recorded permutation.

The report records per-level conditional ECE and confidence coverage, facet coverage, top-level distribution variation, input/recording hashes, requested/reported model identity and prompt/rule versions. The replayed model identity is synthetic-recording. The small synthetic WordLevel tokenizer is diagnostic only; live mode requires a checksummed tokenizer matching the requested model.

[~] 2026-10-09: held-out authorized corpus, metric definitions, accuracy tolerance and review error target require owner approval. Residual risk: these synthetic scores cannot justify release thresholds or automatic processing profiles. No live calls were made; actual provider spend was $0.

Verification: 61 offline scoped tests passed; Ruff and mypy (218 source files) passed. The CLI regenerated the JSON and a same-corpus zero-tolerance diagnostic repeatability gate passed. The regression unit gate rejects a configured top-level accuracy drop. The opt-in provider flow was tested with MockTransport, including three-call admission, unknown-spend retention and sanitized capture.

[~] 2026-10-09: full backend regression relies on CI; available RAM was 2181 MiB, below the 2500 MiB threshold. Residual risk: broader final-state interactions remain unverified locally.
