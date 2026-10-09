# Generation metadata contract proposal (#685)

Status: **Proposed; wire/storage and confidence-policy approval required.**
Date: 2026-10-09. Foundation: #726/#721; independent of child-document #750.
Tracking: [#685](https://github.com/k-sandhu/lumen-copilot/issues/685).
Machine-readable review artifact:
[proposed response schema](../../contracts/proposals/document-metadata.schema.json).
This proposal adds no active endpoint, parser, schema field, migration or filter.

## Why contract review is required

The current `contracts/openapi.yaml` Document response contains filename and
lifecycle dates, with no extracted title/authors/date evidence or metadata
generation projection. The strict chunk search mapping has no document metadata
filter fields. Canonical v1 has generation diagnostics but no typed metadata
contract; #721's reliable language signals are diagnostics, not persisted
permissioned metadata or calibrated date confidence.

#685 explicitly requires storage, permissioned exposure and search filters. That
crosses the canonical/generation, wire and search contracts. Under
[contracts/AGENTS.md](../../contracts/AGENTS.md), "get it reviewed/frozen first"
precedes implementation. The user's instruction for this task says to propose
an unapproved contract change in its PR and continue. No schema is silently
inserted in diagnostics or the active OpenAPI as if it were already approved.

## Proposed observations and title preservation

Keep a separately versioned immutable metadata snapshot bound by Python to the
document's retained generation/source SHA-256. Rust computes only bounded inert
observations from the format's bytes/canonical evidence and supplied file receipts;
it never reads filesystem state, permissions, external identities or databases.
Use the existing facade, stage orchestration and tenant repositories. The response
schema is a wire projection of that snapshot, not a Rust authorization input.

Record title candidates, authors, issuing organisations, date observations,
page/sheet counts and document/block languages. Each observation has a stable ID,
source basis, native field or canonical block/range, extraction method/version and
finite score in [0,1]. Scores describe extraction/evidence confidence, not truth
or authenticity. An explicit calibrated/uncalibrated label accompanies the score.
No numeric weights, date-selection heuristic or reliable-language threshold is
approved by this proposal; schema-validation sample scores are synthetic values.

Extraction never writes the mutable document title or filename. Return candidates
separately. Preserve any user-set title exactly, including during re-ingestion,
metadata retries and parser upgrades; an absent user title does not authorize
automatically promoting an extracted title. The current filename remains the
display fallback. Title-setting API/UI behavior is outside this proposal.

Dates retain raw source value, optional normalized value, semantic kind
(creation/publication/modification/unknown), precision and original timezone
evidence. Basis is `embedded_metadata`, `printed_content` or `file_system`.
The last basis uses only a Python-supplied inert receipt (e.g. field `mtime`),
never upload/lifecycle `created_at` relabeled as source creation or a host path.
Unknown zones/ambiguous dates stay unknown; no guessed locale or UTC conversion.
Printed dates require an exact retained block and Unicode range; embedded dates
identify the format property. Preserve conflicting observations and their IDs,
not a winner. Different semantic kinds are not inherently contradictory; conflict
groups refer to incompatible observations of the same kind with their precision
considered. Unknown/invalid values retain raw evidence without a fabricated date.

Language observations are document-level plus per retained block for mixed text.
Use versioned reliable-language evidence from #721 where applicable and leave
insufficient evidence null; never force a majority language onto every block.
Page/sheet counts are supplied format facts or null, never guessed from lines.
Names, dates and titles remain untrusted source content, subject to normal limits
and output escaping; no connector/identity lookup or model call is introduced.

## Proposed wire surface

After freeze, add `GET /api/v1/documents/{id}/metadata` to active OpenAPI. Optional
`generation_id` selects a retained generation; omission selects the currently
active immutable generation. `block_cursor` and `block_limit` (default 100,
range 1..500) page only block-language entries in stable canonical block order.
The response contains document observations plus `block_languages.items` and a
nullable opaque `next_cursor`; the cursor is bound to document/generation and
ordering and cannot be reused to select another tenant's data. The proposed
response schema has closed objects and explicit nulls for unknown count/language.
Observation strings/collections are bounded; oversized extraction is a typed
budget outcome, not truncated successful metadata.

Use bearer auth and the existing current document visibility predicate, including
mirrored ACL freshness. Invalid/missing/expired auth is 401; foreign/invisible
document or generation is 404. A tenant admin gains no extra document visibility.
Invalid parameters/cursor are 422 using the existing problem+JSON shape.
A visible document without completed metadata returns the complete response with
`status=unavailable`, empty observations and null unknowns, not a fabricated
generation. Therefore generation/source identifiers are nullable only for that
state. Commit the existing `document.viewed` audit event before returning; missing
audit fails closed. No new audit taxonomy or write endpoint is needed for this
read-only projection. No contents/values appear in operational logs/audit metadata.

## Persistence and filter proposal

Owner must approve retained-generation identity/retention together with the
metadata snapshot binding; #726's operational checkpoints are not a retained
generation policy. Persist schema/method/build/source identities and checksum with
the generation through tenant-scoped repositories, using a reversible migration
and attempt fence. Replace only a new generation's metadata, never old sources or
user title fields. Delete snapshots when their owning document is deleted; honour
the approved generation retention policy. No other tenant's snapshot is reusable.

After approval, project snapshot identities into chunks at publication and add
strict search mappings within `backend/app/search/`. The metadata filters are
additional AND predicates inside both lexical and vector retrieval legs, combined
with existing tenant/ACL filters, never a caller-provided alternative allow-set.
Proposed query fields: `metadata_language` (reliable document ISO-639-3 code),
`metadata_author` (exact supplied value), and a grouped `metadata_date` object with
kind, basis, inclusive from/to and minimum confidence. A date match must satisfy
all predicates on the **same** observation via a nested index mapping; separate
array fields must not manufacture a match from two conflicting dates. Any
matching retained observation qualifies; no hidden "best date" is selected.
Absent observations do not match; malformed/reversed ranges are 422. Filter
contracts and exact precision/normalization semantics must be frozen in active
OpenAPI before implementation. Do not add date filters to the existing lifecycle
timestamp fields or infer a publication date from ingestion time.

Publish new snapshot/chunk projection only after existing refreshed-index and
attempt-fencing gates. Existing unprojected chunks remain readable without these
filters; they do not match new filters. Rollout/backfill is explicit and bounded.
Metadata extraction/shadow/filter exposure each defaults OFF in configuration
until approved. Native failures leave Python evidence/title unchanged. #685 does
not enable another format, select a parser, or change classification contracts.

## Required acceptance after approval

Test-first generated property/content fixtures must cover titles and multiple
authors, all three date bases, timezone absence, ambiguous/invalid dates, conflicting
same-kind dates, mixed document/block languages and supplied/unknown counts.
Property tests must resolve every printed evidence range to the exact Unicode
slice of the retained block/source and keep observations stable across round trips.
Root tenant/visibility, stale ACL, revoked grants, expired auth, bad cursor/limits,
wrong generation and missing audit negatives must fail through existing chokepoints.
User title remains byte-for-byte unchanged after re-ingestion/retry/upgrade.
Snapshot checksum/attempt/deletion tests and fake search tests must prove nested
same-observation matching in both legs. Live RLS/index checks await an authorised
environment; no live tests or Docker actions are run for this task.

| Fidelity dimension | Current evidence |
|---|---|
| Response structure, bounds and unknown states | Proposed schema validation only |
| Title preservation and conflict recording | Specified; extraction/persistence unimplemented |
| Date confidence/basis and exact printed offsets | Specified; calibration/extraction unmeasured |
| Document/block language and source counts | Existing #721 diagnostics; new metadata projection unimplemented |
| Permissioned reads and nested metadata filters | Proposed; runtime tests pending freeze |

## Owner decisions

Verification on 2026-10-09:
- [x] Draft 2020-12 schema compilation, three generated positive review shapes
  (available, conflicting dates, unavailable), ten generated structural negatives
  (confidence bounds, missing printed evidence, unknown fields, inconsistent
  unavailable identity, invalid hash/UUID, conflict cardinality and page size),
  UTF-8/local-link checks and `git diff --check` passed.
- [x] `cargo deny --manifest-path rust/Cargo.toml --config rust/deny.toml check
  licenses advisories` passed after a 2984 MiB RAM check; no dependencies changed.
- [~] 2026-10-09: extraction/property, persistence, auth/audit and retrieval tests
  pending contract freeze; no runtime implementation added. Residual risk:
  structural schema checks do not prove metadata fidelity, permissions, score
  calibration, title preservation or filter behavior.
- [~] 2026-10-09: full backend suite, live datastore checks, packaging and measured
  baseline evaluation deferred; this PR changes proposals only. No Docker actions.
  Residual risk: metadata remains unavailable through the runtime.

Approve/revise the snapshot and read/filter contracts; retained generation binding
and retention; confidence method/calibration and date precision/ambiguity semantics;
and the rollout/backfill budget. Implementation, migrations, extraction tests and
production exposure remain pending those approvals. #682/#697 remain on their
separate #750 gate; #677/#678 have independent candidate/spike PRs.

Merge gate: hold until measured against the baseline evaluation; a human merges.
