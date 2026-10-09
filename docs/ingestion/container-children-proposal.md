# Container child-document contract proposal (#682)

Status: **Proposed; owner approval required before implementation.**
Date: 2026-10-09. Tracking: [#682](https://github.com/k-sandhu/lumen-copilot/issues/682),
with [#697](https://github.com/k-sandhu/lumen-copilot/issues/697) as its dependent.
Foundation: [#726](https://github.com/k-sandhu/lumen-copilot/pull/726), commit
`90f08db042638b64de0094d7cc9e2da1675bec6d`, which includes #721.

This document proposes an internal computation contract. It changes no executable
schema, API, parser, configuration, persistence or permission behavior.

## The missing boundary

[Canonical v1](canonical-schema-v1.md) represents one document. `Block.parent_id`
links blocks within that document; it is not a document relationship. Its typed
`PartKind` permits page, slide and sheet. `SourceRegion.name` can label a location,
but does not identify an independently rendered child, its original bytes, or its
permission root. Rust rejects unknown fields and schema versions other than 1.
The Python canonical view likewise has no document-level parent/child field.

The relational `Document` has no parent-document key. The
[resumable stages](../specs/0027-resumable-ingestion.md) cache outputs against a
tenant-bound document and attempt; cached output is explicitly not an authority
for permissions or historical citations. None of these contracts specifies how
independent child renderings become permissioned, retained citation sources.

Putting member text into a root block, inventing a source-part enum, or hiding
lineage in diagnostics would not establish that boundary. The task expressly
requires a proposal and a stop if approval is needed; root AGENTS.md sections
4 and 8 require confirmation before implementing unspecified behavior.

## Smallest proposed contract

Keep canonical schema/renderer v1 and all single-document parser signatures.
Add a separately versioned internal `DocumentBundle` for container formats:

```text
DocumentBundle {
  bundle_schema_version: 1,
  root: DocumentV1,
  children: ChildDocument[]
}
ChildDocument {
  id: string,
  parent_id: string,
  relation: archive_member | email_attachment | mailbox_message,
  locator: ChildLocator,
  source_sha256: lowercase hexadecimal SHA-256,
  document: DocumentV1
}
ChildLocator {
  ordinal: positive integer,
  archive_member_path: string | null,
  mime_part_path: positive integer[] | null,
  display_name: string | null,
  display_name_origin: source | derived | null
}
```

`DocumentV1` is exactly the existing canonical input shape; the existing renderer
validates and renders each root/child independently. A bundle carries no raw
member bytes, tenant/owner identifiers, grants, credentials or storage capability.
It is parsed with collection/string preflight and bounded validation before
allocating model collections, as canonical v1 already does for its JSON boundary.

Reserve the root ID `root`. Children form a flat ordered list; a parent must be
`root` or an earlier child. IDs are ordinal chains (`c1`, `c1.2`), derived from
container encounter order, not filenames or hashes. Ordinals include unsupported
members, so changing parser availability does not renumber later members. Reject
duplicate IDs, missing parents, inconsistent ID/ordinal chains and excess depth.
Render/parse completion order cannot change this list. Identical member bytes in
different locations remain distinct children. Each child hash must match both its
decoded original bytes and its canonical generation's source hash.

Archive locators require `archive_member_path`, with `mime_part_path=null`.
Email attachment locators require a MIME part path relative to their immediate
message; paths number parts from 1, with `[1]` identifying a single-part message.
Mailbox-message locators use their encounter ordinal; both path fields are null.
For a nested member, follow parent links to reconstruct the complete structured
container path. For example, `c1` at `reports.zip` and `c1.2` at `a/notes.txt`
produce `['reports.zip', 'a/notes.txt']`. A display delimiter is never an identity.
MIME paths and filenames remain distinct; an attachment filename is inert metadata.

This requires approval of a **container-only return-type exception** to the task's
single-document entry point: `bytes + limits -> Result<DocumentBundle, Error>`.
Ordinary parsers continue returning `DocumentV1`. If the owner requires the same
return type for every format, a canonical v2 with explicit document children is
the alternative; do not extend v1 silently or implement both contracts.

## Permissions, offsets and persistence

Propose logical child documents inside the root generation, with no independently
grantable/deletable `documents` rows in this first phase. Python binds the whole
bundle to the already authenticated root's tenant, document and ingestion attempt.
Every retrieval, direct fetch and citation resolution applies the root's **current**
visibility predicate, including mirrored ACL freshness. A cached allow-set or a
filename/header supplied by the container cannot grant access. Revoking root
access excludes every descendant; deleting the root invalidates every child.
This follows [INV-1/INV-2](../specs/0004-security-and-domain-invariants.md).

Offsets remain local to each retained child rendering. The evidence identity is
`(root_document_id, retained_generation_id, child_id, block_id, char_start,
char_end)`. Root evidence uses `child_id=root`. The exact Unicode invariant is
`child_rendered_text[char_start:char_end] == evidence_text`. Archive/MIME locators
are additional provenance, never offsets or filesystem locations. Do not
concatenate children into the root source or borrow offsets from display labels.
No existing citation may be retargeted to a new child or generation.

The native/shadow result may use this internal bundle after approval. Production
cutover must wait for a separately reviewed additive storage/citation contract
that retains each child's source rendering and locator and resolves the above
identity through current root permissions. The existing operational checkpoint
is insufficient for retention. This proposal does not introduce an endpoint or
claim that child search/publication is already supported. Independent child rows,
child grant management and standalone child controls require another decision.

## Runtime and stage integration proposed for #682

Expand and detect under one root `Context`: deadline, cancellation, accounted
memory, output and work budgets are shared by all descendants. Use the existing
bounded runtime pool and bounded in-flight windows; never create a pool per
member. Reserve decompression buffers and result metadata before allocation.
The archive counters count cumulative expanded bytes and encountered members at
every nesting level, including unsupported members; nesting cannot reset limits.

Propose additional configurable caps for member bytes, total expanded bytes,
member count, expansion ratio and nesting depth. Ratio uses actual expanded bytes
against compressed payload bytes; a zero denominator with nonempty output fails.
Check per compressed member and over the root expansion tree, using checked
arithmetic. TAR has no compression ratio; its size/count/depth limits still apply.
GZIP, including tar.gz and concatenated streams, cannot bypass either counter.
Directories/headers still consume work and entry limits. Numeric production
capacities require owner evaluation; this proposal assigns no approved defaults.

Archive names are validated before member decoding: reject absolute/drive/UNC
paths, backslashes, NUL/control characters, empty/dot/dot-dot components and
duplicate member paths. Preserve accepted Unicode names exactly. Reject links,
device/special entries and encrypted members; never extract to the filesystem.
A missing GZIP filename uses a derived `payload` display name. Unsafe names are
rejected rather than repaired into a different evidence identity.

Proposed safe typed outcomes: `unsafe_archive_path`, `archive_link_rejected`,
`encrypted_archive_member`, `archive_expansion_limit`, `archive_ratio_limit`,
`archive_member_limit`, `archive_depth_limit`, plus existing invalid/parse,
unsupported, cancellation, deadline, memory and internal error categories.
Errors carry codes and bounded inert member identifiers, never raw contents,
machine paths or native exception payloads. Unsupported member formats are
explicit incomplete regions, not successful empty children. A container with
skipped members is partial; security violations or exhausted budgets reject the
whole bundle and publish no descendants. Empty containers are non-searchable.
These are proposed semantics, awaiting approval alongside the bundle contract.

#726 remains Python-owned: one aggregate checksummed extraction bundle belongs
to the root's fenced attempt, including bundle version, parser/build identities,
format gates and all container limits in its fingerprint. A changed limit or
source invalidates the aggregate and downstream stages. Retry cannot reuse a
different root/attempt's children. No child is independently published before
the complete approved root outcome. Aggregate checkpoint size remains bounded.
Per-child resume checkpoints are outside this initial proposal.

New archive/email acceptance, native candidate and cutover switches must each
default OFF. Python remains authoritative; Rust failures in shadow mode cannot
alter the Python result. Reuse #729's HTML extractor only on #697's branch after
this approval; keep dependencies explicit in that PR.

## Acceptance and verification required after approval

Generated ZIP/TAR/GZIP fixtures must cover nested containers, Unicode names/text,
multiple children and stable parallel ordering. Property tests must verify full
structured archive/MIME locators and exact local Unicode slices, including two
children with identical bytes. Generated negative fixtures must cover every
path/link/encryption rejection, declared-versus-actual expansion, zero ratio
denominators, aggregate nested counters, depth boundaries and cancellation.

Offline Python tests must prove root-to-descendant visibility, foreign-tenant 404,
revocation/freshness denial, deletion cleanup, stale-attempt fencing and no partial
publication after a fatal member. Retained citation round trips require the
approved persistence contract; checkpoint reuse alone is not evidence of them.

| Fidelity dimension | Current result |
|---|---|
| ZIP/TAR/GZIP member extraction and order | Unmeasured; parser not implemented |
| Exact archive paths and Unicode child offsets | Unmeasured; property tests await approval |
| Email attachments and MIME paths | Unmeasured; #697 not started |
| Permission inheritance and retained citations | Unmeasured; binding/storage contract pending |
| Throughput, peak memory and baseline parity | Unmeasured; no benchmark arm added |

- [x] Proposal verification: UTF-8 reads and all local Markdown links passed;
  source inspection confirmed the missing document relationships and tenant-bound
  stage key. `git diff --check` passed. `cargo deny --config deny.toml check
  licenses advisories` passed at 3728 MiB available RAM (one unused licence
  allowance warning); the existing lockfile and dependencies are unchanged.
- [~] 2026-10-09: executable contract, parser tests and backend suites
  deferred at the explicit contract-approval stop; no code/dependency changes.
  Residual risk: all archive/email behavior and fidelity remain unimplemented.
- [~] 2026-10-09: live Postgres/OpenSearch and Docker actions excluded by the task.
  Residual risk: live persistence/RLS/search behavior remains unverified.

## Owner approval needed

Approve or revise the bundle shape and container-only return type; logical children
bound to the root's current permissions; local child offsets and retained evidence
identity; and fail-closed versus explicit-partial member outcomes. After approval,
implement #682 test-first, then #697, #677, #678 and #685 in the requested order.
The latter three issues are untouched at this stop, not considered completed.

Merge gate: hold until measured against the baseline evaluation; a human merges.
