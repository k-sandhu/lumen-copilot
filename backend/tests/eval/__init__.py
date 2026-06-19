"""Grounded-chat evaluation harness (#29, ADR-0003 §9, backend/AGENTS.md).

"Eval from day one." This package is the executable proof of the M1 headline —
a grounded chat answer that is **permissioned, cited, and verifiable** — and the
quality guard that fails when an answer stops being grounded.

It is built to run two ways from one golden set, mirroring the existing
live-skip pattern:

* **Offline / deterministic** (CI-safe, no key, no stack): a faked gateway and a
  faked retrieval over the golden corpus drive the real metrics
  (:mod:`tests.eval.metrics`); the thresholds in :mod:`tests.eval.scoring` are
  asserted. This exercises the harness itself on every run.
* **Live** (key + running stack): the real OpenRouter model and the real
  pgvector hybrid retrieval over the *same* seeded corpus, skipped cleanly when
  ``OPENROUTER_API_KEY`` / Postgres are absent.

Three metrics, one per mission concern (spec 0004 INV-2/INV-3, ADR-0003 §9):

* **groundedness** — every asserted fact carries a citation (an unsupported
  claim FAILS);
* **citation-correctness** — each cited passage actually matches an expected
  source (a citation to the wrong / a non-permitted passage FAILS — INV-3);
* **retrieval-recall** — the expected supporting passages were retrieved.

The prompt under evaluation is pinned in-repo
(:data:`app.services.prompts.grounded_answer.PROMPT_VERSION`) so a behaviour
change is attributable to a specific prompt revision.
"""
