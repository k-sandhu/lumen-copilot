# Singleton taxonomy branches — #745

ADR-0028 descends only the selected branch. A branch with one child has no
decision ambiguity: record that sole child with conditional probability one,
retaining the parent's path probability. Do not fabricate an extra alternative
or send a one-option choice that violates the gateway's validated input contract.

At the type level, independent facet predicates still run in one request. If
the type has only one possible child, this request contains predicates alone.
Per-level persistence, unknown facet handling, accounting and failure status
follow the existing stage contracts. No sibling outside the chosen branch is
evaluated. A facet failure preserves the already completed parent levels.

Regression acceptance: the `other/other/other` path completes with one root
choice request and one request containing all facets; its family/type sibling
distributions are deterministic, and total cost includes exactly both requests.

The classifier version is `classification-1.1`, shared by the stage result and
repository work fingerprint. The changed-only bulk job therefore re-schedules
older failed inputs after the correction, while preserving explicit overrides.

Verification: both regressions failed first for missing behavior. The offline
stage/persistence subset passed 14 tests with two workers. No live calls ran.

[~] 2026-10-09: full regression relies on CI because available RAM was below
2500 MiB. Residual risk: broader final-state interactions remain unverified locally.
