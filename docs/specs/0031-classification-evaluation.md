# Classification evaluation — #694

ADR-0028 requires measurement before confidence is trusted. This offline diagnostic
harness replays checked-in synthetic response recordings through the real decisions
gateway and classifier. Recordings are scripted fixtures, not live provider quality
measurements. Larger authorized labelled corpora stay outside this public repository.

Metric definitions proposed for owner approval:

- Per-level accuracy is unconditional path-prefix accuracy over every gold example
  with that depth. Abstentions and missing deeper levels remain incorrect in the
  denominator; wrong-parent errors propagate. Report sibling-conditional confidence
  calibration separately from the product confidence for the complete path.
  Conditional ECE includes deeper predictions only when their parent is correct;
  coverage exposes the excluded wrong-parent/abstaining examples.
- Facet positive-class F1 is macro-averaged across supported labels with nonzero
  TP/FP/FN denominator. Unknown gold is excluded; unknown predictions on positive
  gold are false negatives. Report unknown prediction rate/coverage and unsupported
  labels explicitly; unknown predictions never become false labels.
- Expected calibration error uses explicit equal-width bin count, weighted absolute
  difference between mean confidence and empirical accuracy. Include confidence
  coverage and both per-level and complete-path calibration.
- Order sensitivity compares fixed and a recorded seeded permutation: path flip
  rate and mean total-variation distance over aligned top-level distributions.
- Latency includes the whole cascade. Replay fixture latency is explicitly simulated;
  wall-clock replay timing is separate. Cost includes every attempt and distinguishes
  zero from unknown spend, including failed primary requests before fallback.

The baseline gate requires an explicit top-level accuracy tolerance and matching
corpus fingerprint, metric definition/bin count, taxonomy and sample count. A drop
greater than the tolerance fails. No default tolerance exists.
The corpus fingerprint covers labels and bounded input evidence, excluding response
recordings so that changed predictions remain comparable. Recording hashes are
separate provenance. Mode/model/method/prompt/rule versions remain in the report.

Calibration requires an owner-approved configuration (identity/date, corpus hash,
bin count, regression tolerance, review error target, minimum support and confidence
level). Thresholds are derived per method/model/taxonomy from independent calibration
samples: choose the lowest observed confidence whose accepted set's Wilson upper
error bound meets the target and minimum support. Held-out samples measure the
selected threshold without participating in its selection. Synthetic-only or
insufficient corpora produce no approved threshold. Artifacts record provenance,
sample counts, measured errors and taxonomy/model/method; no production cutoff changes.

Live mode is explicit, restricted to one bundled synthetic invoice, no retries or
fallback, at most three requests and conservative held spend below $0.05. It requires
a checksummed model-matched tokenizer. The key comes through Settings, stays ephemeral,
and is never output. Captures project only validated answers/model/usage and safe
attempt/order metadata; raw errors, request headers and credentials are excluded.

[~] 2026-10-09: release corpus, metric definitions, accuracy tolerance and review
error target await owner approval. Residual risk: synthetic diagnostics cannot
establish real-world quality or trustworthy review thresholds. All production
model classifications remain advisory/review-required.
