# Legacy DOC/PPT spike decision (#678)

Status: **Deferred extraction; typed unsupported outcome implemented for review.**
Date: 2026-10-09. Foundation: #726/#721, detection #704 and runtime #705.
The task explicitly permits a decision record plus typed
`unsupported_legacy_format` when extraction is not small and safe.

## Options and generated evidence

| Criterion | Native CFB plus DOC/PPT format parsing | Isolated conversion worker |
|---|---|---|
| Current capability | Existing cfb 0.15.0 and detector recognize container stream names; neither is a DOC/PPT canonical parser | No installed converter executable was found; no worker was deployed |
| Generated fixtures | Tests create DOC/PPT-shaped CFB containers with header/stream markers and Unicode text bytes, then assert recognition and safe unsupported outcomes | Same fixtures are candidates for a later worker experiment; conversion is unmeasured |
| Fidelity work | Requires format-specific record validation, text decoding, ordering, tables/notes and exact mapping; scanning strings cannot establish these properties | Conversion may alter structure, original locations and metadata; a map back to original evidence still requires evaluation |
| Isolation | Pure in-process computation; shared limits are cooperative, and CFB dependency parsing is not a hard process deadline | Proposed supervisor would enforce no network, one tenant/attempt input, no host/data mounts, memory/CPU caps and process-tree termination on deadline |
| Footprint/cost | cfb is already locked; this refusal path adds no dependency or deployed image. Incremental whole-image size and RSS are unmeasured | Converter binary, fonts/data and worker image are unselected; image size, RSS, cold/warm time and CPU cost are unmeasured |
| Licences | Existing cfb: MIT; locked graph checked with cargo-deny | Engine, fonts, data and transitive libraries require independent permissive-only review; no engine is approved by this record |
| Adversarial tests | Generated corrupt/non-CFB input, input/memory/work/deadline/cancellation limits, explicit refusal and safe bridge error | Network denial, foreign-tenant data absence and hung-process kill must be tested before selection; not claimed here |

The generated CFB fixtures exercise recognition and refusal only. They are not
complete valid legacy documents and do not constitute successful DOC/PPT
extraction fidelity. No third-party documents are used. No conversion process,
network call or Docker action was performed in this spike.

## Decision

Do not select a production native parser or a conversion engine. A complete
format reader with faithful original provenance is not a small addition to the
existing detector. An untested converter would move trust and introduce an
external execution boundary without evidence of isolation, licensing or mapping.
Both require an evaluated follow-up decision; neither meets the current merge
gate by merely returning plausible text.

Return a distinct `CoreError::UnsupportedLegacyFormat`, safe message/code
`unsupported_legacy_format`, and a Python
`DocIntelUnsupportedLegacyFormatError` subclass of the existing unsupported
exception. The pure byte+limits entry validates the bounded CFB container only
to recognize DOC or PPT; it never publishes text or an empty canonical success.
Other CFB families remain generic unsupported. Deadline/cancellation and input,
work and estimated-memory accounting apply before detection and again after it.
An engineering candidate cap of 256 KiB limits dependency work; this is a spike
profile, not an approved production capacity or proof of a hard CFB deadline.

The Python facade has default-OFF acceptance/shadow/cutover switches. Enabling
them cannot make an unavailable parser live: route remains unsupported and an
explicit accepted evaluation call returns the typed refusal. Python remains
authoritative; no upload allowlist, persistence, tenant grant or source offset
changes. A single file remains a single document; #750 is not a dependency.

## Follow-up owner decision

Select an evaluation path only after complete generated DOC/PPT fixtures and
held-out authorised evaluation, exact native-location mapping, resource metrics,
licence review and adversarial isolation checks are available. A converter must
run under a Python-owned process supervisor with a wall deadline and forced
process-tree kill, without network or other tenants' inputs; thread cancellation
is insufficient. Neither network isolation nor hang termination is verified by
the current refusal path. Original issue extraction acceptance remains deferred
under the explicit scope exception in the task.

Merge gate: hold until measured against the baseline evaluation; a human merges.
