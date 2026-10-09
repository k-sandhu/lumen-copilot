# Classification surfaces — #693

The additive OpenAPI proposal requires owner review/freeze under ADR-0006 and
`contracts/AGENTS.md` before API, agent-tool or UI implementation. The proposal
defines a content-free projection, permissioned reads, audited owner/admin
overrides with revision fences, and prefix/facet narrowing. The response adapter
must explicitly project the listed fields; internal attempt accounting is omitted.
Generated TypeScript is regenerated locally, never committed. Existing UI mirrors
do not consume these new optional fields until the contract is approved.

Internal search implementation acceptance criteria:

- Index only completed classifications or explicit overrides. Pending/incomplete
  and missing values do not imply false facets or a default document type.
- Store ancestor node IDs, known true/false facet terms, taxonomy version,
  confidence and method. Never store classification excerpts or credentials.
- Both hybrid legs append metadata narrowing after mandatory tenant/ACL clauses.
  A prefix matches the exact node and its descendants at slash boundaries.
- SQL permission hydration runs before a batched current-classification recheck;
  stale index metadata cannot admit a document that no longer matches.
- Classification completion queues index repair after commit, independently of
  readiness. Repair uses the existing idempotent sync path and backfill.
- Filters cannot change permissions, source text, chunk boundaries or citations.

[~] 2026-10-09: API routes, UI badge/confidence/override states, agent-tool wire
arguments and processing-profile application await owner contract freeze and
profile configuration approval. Residual risk: internal capabilities have no
user-facing entry point; class-dependent processing is not enabled. ADR-0028
  requires review/calibration before applying processing profiles.

Verification: 66 scoped offline checks passed. The full offline run reported
3758 passed, 18 skipped, 1 existing xfailed and two failures. The new clock-sensitive
assertion was corrected (10 classification-search tests passed); the inherited
settings partition failure was corrected separately in #742 (383 connector checks
passed, 9 capability skips). Ruff, mypy (218 files), API type generation and Compose
configuration validation passed. No live calls or datastore tests ran.
After integrating #745's separately tracked singleton correction, 42 scoped
search/stage/persistence/authorization/index-sync tests passed. Four contract
projection regressions passed for null method and decimal accounting (including
scientific notation), after failing first. API types were regenerated again.

[~] 2026-10-09: final full-suite verification relies on CI under the one-full-run
limit. Residual risk: broader final-state interactions remain unverified locally.
