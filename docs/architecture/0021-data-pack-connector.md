# 21. Data packs as a connector — curated document sets as a managed source

- **Status:** Proposed *(surface decided by sponsor 2026-07-30: connectors, not Settings; design proposed)*
- **Date:** 2026-07-30
- **Builds on:** [ADR-0004](0004-architecture-boundaries-and-adapters.md) (the `connectors/` boundary), [ADR-0009](0009-connector-framework-and-web-source.md) (connector framework, `sources` model, SSRF chokepoint), [ADR-0019](0019-connector-sdk-and-oauth.md) (SDK widening: binary payloads, `external_id` reconcile), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1/INV-2 scoping, INV-6 audit, INV-7 tiers)

## Context

A **data pack** is a curated, checksum-pinned set of real documents that gives a team a realistic corpus for its domain in one action — five industry packs today ([#443](https://github.com/k-sandhu/lumen-copilot/issues/443)) plus two tax-research packs ([#515](https://github.com/k-sandhu/lumen-copilot/issues/515)) covering US-federal + New York State and Canadian-federal + Ontario filing.

**Today a pack has no route into the product.** `tests/eval/benchmark/load_pack.py` ([#441](https://github.com/k-sandhu/lumen-copilot/issues/441)) is host-side developer tooling: it logs in over the API and `POST`s each file to `/api/v1/documents` under a collection. Using it needs a repo checkout, `uv`, and the user's password on a command line. Nothing in the SPA can install a pack.

Three things make "just upload the files" insufficient rather than merely inconvenient:

1. **A pack is a unit, and loose documents aren't.** A tax pack is 26–36 files. Uploaded individually they become an undifferentiated pile in Documents with no provenance beyond a filename, no way to see what came from where, and no way to remove the set again.
2. **Refresh is a first-class requirement.** Packs deliberately carry **rolling** entries — the current-year IRS 1040 instructions, the NYS sales-tax and withholding publications, the consolidated Canadian *Income Tax Act* — precisely because tax content is re-published as law changes. `load_pack --refresh` exists for this. Uploaded documents have no refresh affordance at all; the user would have to notice, delete, and re-upload by hand.
3. **Installing is a costly, side-effecting action.** A pack install writes dozens of documents, triggers Celery ingestion, and consumes embedding and storage budget. That is content provisioning, not configuration.

**Where should the surface live?** `/settings` holds three per-user preferences (default model, custom instructions, avatar); point 3 rules it out — a pack install would be the one heavy, hard-to-undo button on a page of toggles. `/documents` is where uploads land, but it models *files*, not a refreshable set with an origin.

`/sources` already models exactly what a pack is: an external body of content with provenance, health, an indexed count, a last-synced stamp, and re-sync/remove actions. **Sponsor decision (2026-07-30): data packs are connectors.**

## Decision

1. **A data pack is a source.** New connector `backend/app/connectors/datapack/` exposing a module-level `CONNECTOR`, picked up by the existing auto-discovered registry ([ADR-0008 §3](0008-conflict-free-parallel-delivery.md) / [ADR-0009 §1](0009-connector-framework-and-web-source.md)) — no shared file is edited. `SourceType` gains `datapack` **additively** (`[web, gdrive, datapack]`), the extension path ADR-0009 §6 reserved for exactly this.

2. **The catalog is server-owned; the user supplies an id, never a URL.** `config: {pack_id: string}`. `validate_config` rejects an unknown id as `ConnectorConfigError(code="unknown_pack")` → **422** (INV-8). This is the load-bearing difference from `web`: because every URL originates in a code-reviewed, server-side manifest, **a data pack has no SSRF surface** — there is no user-controlled fetch target to defend. Fetches still go through the hardened `connectors/` fetch path (scheme allowlist, size and time caps, content-type allowlist, required `2xx`, descriptive `User-Agent`, per-tenant rate limit) so the guarantees of ADR-0009 §3 hold uniformly rather than being re-implemented.

3. **Sync is "fetch the pack's pinned entries."** Each manifest entry becomes one `FetchedDoc` using the [ADR-0019 §3/§5](0019-connector-sdk-and-oauth.md) widening — no new SDK surface is required:

   | `FetchedDoc` field | value |
   |---|---|
   | `data` + `mime_type` | the raw bytes and declared type, so PDFs/XLSX go through the **real ingestion parsers** rather than a text pre-pass |
   | `external_id` | the manifest `file_id` — a stable identity per pack file |
   | `url` | the upstream source URL, carried for provenance and citations |
   | `modified_at` | last-seen upstream stamp |
   | `title` | the manifest's human document name |

   Integrity: a **pinned** entry whose sha256 does not match `checksums.json` is rejected (`checksum_mismatch`) and reported as a failed file — it is never ingested. A **rolling** entry has no pin to enforce; its observed checksum is recorded as last-seen (see the risk in *Consequences*).

4. **Re-sync is the refresh mechanism — no new endpoint.** Because `external_id` gives identity-based reconcile ([ADR-0019 §3](0019-connector-sdk-and-oauth.md)), `POST /sources/{id}/sync` re-fetches and updates only the entries whose bytes changed. That is precisely `load_pack --refresh` semantics, expressed through the connector framework instead of a bespoke CLI flag. `DELETE /sources/{id}` already cascades the source's documents, which gives "uninstall the pack" for free.

5. **Install scope follows who installed it.** ~~Per-user only; tenant-wide provisioning deferred.~~ **Superseded by [ADR-0022 §6](0022-group-access-model.md) (2026-07-30)**, which supplies the sharing model this clause said did not exist: a pack added by a **user** is owner-scoped (private); a pack added by an **admin** is visible **tenant-wide** or to a named **group**. Pack content carries no bespoke permission code — visibility rides the existing collection-grant cascade, so INV-1/INV-2 are enforced by the same chokepoint as every other document. The Sources grid shows the scope on the existing `PermissionPill`.

6. **A read-only catalog endpoint feeds the Add-source flow.** `GET /source-catalog` returns the installable packs — id, name, description, publisher/rationale, file count, approximate download size, and (for tax packs) their topic coverage. Tenant-agnostic and unauthenticated-equivalent in content, so it is cacheable; it exposes no tenant data. The FE renders it inside the existing `AddSourceModal` shape rather than inventing a second add flow.

7. **Add/sync/remove stay tier-T1 owner-gated and audited**, unchanged from ADR-0009 §5 — a pack install is a write, and every add/sync/delete emits an audit event (INV-6).

8. **Prerequisite: the catalog moves out of `tests/`.** The manifest and pack catalog live in `backend/tests/eval/benchmark/{manifest,packs}.py` today. Production code must not import from `tests/`, so the catalog moves to `backend/app/connectors/datapack/catalog.py` and the benchmark tooling imports it — **reversing today's dependency direction**. The benchmark's checksum pins, licence/provenance records, and topic-coverage validation move with it; `download.py`/`load_pack.py` become thin clients of the shared catalog. This is a real refactor and a named precondition of the connector, not a tidy-up to do afterwards.

9. **Deferred (separate decisions):** tenant-wide pack provisioning (§5); user-authored or uploaded packs; scheduled auto-refresh — refresh stays **on demand only**, consistent with [#443](https://github.com/k-sandhu/lumen-copilot/issues/443).

## Consequences

- **The Sources surface absorbs packs with almost no new UI.** `SourceCard` already renders a glyph, a sync-health `StatusDot`, an indexed count, a `PermissionPill` and a `FreshnessPill`, and already offers re-sync and confirm-gated remove. A pack card is those same fields with different data; the only genuinely new UI is catalog browsing in the add flow.
- **Refresh semantics unify.** `--refresh` and connector re-sync stop being two mechanisms for one idea. `load_pack.py` becomes a thin client over the same catalog, or is retired once the UI lands — either way the CLI stops being the only way in.
- **No SSRF surface, but a supply-chain one takes its place.** The server now fetches on a schedule the user triggers, from a fixed set of government and standards hosts. Checksum pins are the integrity control for pinned entries. **Rolling entries deliberately trade that pin for currency** — their bytes are unverifiable by construction, so a compromised upstream would be ingested. Mitigations are the shared fetch chokepoint (host is manifest-fixed, magic-byte check, size and content-type caps) plus the fact that rolling entries are a small, explicitly-marked minority. This is a recorded, accepted risk, and the reason benchmark questions may never cite a rolling file.
- **Install cost becomes visible.** A pack is tens of MB and dozens of ingest+embed jobs. The catalog endpoint carries file count and approximate size so the user sees the cost before installing, and per-file failures surface as a partial-sync state rather than an all-or-nothing error.
- **The `tests/` → `app/` move (§8) must land first** and touches the benchmark suite, so it is sequenced as its own change with the benchmark tests green on both sides.
- **A pack's documents are ordinary documents.** They chunk, embed, retrieve, cite and audit exactly like an upload — no parallel content path, and no change to retrieval or the permission filter.
- **Delivery follows the ADR-0008 M2 shape:** serialized prep (this ADR → [ADR-0022](0022-group-access-model.md) access model → catalog move → `/sources` + `/source-catalog` contract change → `SourceType` migration) then parallel build (datapack connector BE ‖ catalog browse FE), per [ADR-0006](0006-contract-first-parallel-implementation.md). The access model lands **first**: an admin-installed pack has nowhere to be visible until source visibility exists.
