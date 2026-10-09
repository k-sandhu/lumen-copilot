# ADR-0029 — Selective OCR through a domain provider interface

Status: Proposed — sponsor-directed #695; baseline evaluation and human acceptance required before merge.
Date: 2026-10-09. Epic: #661. Depends on #737 and #726. Builds on ADR-0027, ADR-0025, ADR-0004 and spec 0004.

## Decision

Python owns a domain-only OcrProvider interface and tenant-bound durable accounting.
The OpenRouter implementation lives in app/llm/ and uses httpx against
POST https://openrouter.ai/api/v1/chat/completions with file-parser/mistral-ocr.
Rust selects only diagnostic needs_ocr pages, uses PDFium in the existing
OS-limited worker to produce one-page PDFs, and merges annotation text without
normalizing Unicode or inventing boxes, layout or confidence. Standalone PNG,
JPEG, WEBP and TIFF images use a bounded Rust decoder and one-page PDF wrapper;
multi-frame TIFF is rejected rather than silently dropping frames.

As verified in the [OpenRouter PDF documentation](https://openrouter.ai/docs/guides/overview/multimodal/pdfs)
on 2026-10-09, the parser price is $0.002/page ($0.0022 for US-region requests),
including BYOK. Chat input/output charges are additional. The deployment selects
a cheap chat model and a conservative total per-page ceiling; max_tokens is 1.
The model answer is discarded. Only file annotation text is evidence, including
error.metadata.file_annotations when chat inference fails. No automatic retry,
engine fallback or public file URL is permitted. Images in annotations are ignored.
The provider documents no page boundaries or bounding boxes: each request has one
page, geometry is unknown and confidence is null. The parser forwards at most
eight images per PDF. No OCR accuracy claim is made.

Pages leave the tenant boundary and are processed by OpenRouter, its parser and
chat provider. Deployment enablement never enables a tenant. A tenant policy row
must explicitly opt in with attributable administrator approval and positive
page/cost/concurrency limits; missing policy is OFF. Operator provisioning through
the repository is the initial control seam; no new admin UI is introduced. Hosted
processing must not be represented as offline or as having verified retention,
regional guarantees or zero retention. Owners decide deployment/provider terms.

The cache key is (tenant, SHA-256 of submitted one-page bytes, engine/profile).
PDFium-generated trailer IDs are replaced with deterministic source/page IDs,
and the fresh wrapper creation date is fixed before hashing or dispatch,
preserving byte lengths and cross-reference offsets. Source content and its
original metadata are never rewritten by this wrapper-metadata operation.
No cross-tenant cache reuse. Reserve a page and the total ceiling in a short durable
transaction under a tenant policy lock, and commit a content-free OCR intent audit
before dispatch. Known results and actual usage commit before canonical merging.
Reported costs above the reservation are recorded and fail closed before publication;
subsequent admission uses that actual spend. A local reservation is not a provider
price cap: owners must provision a restricted key/model with an adequate ceiling.
The same page is never dispatched twice automatically, including concurrent
workers and ingestion retries. An ambiguous timeout/crash keeps its reservation
and blocks repeat payment; operator reconciliation is required. A missing cost
retains the ceiling. Lifetime page/cost accounting is conservative; there is no
implicit reset or refund after a potentially billable request. Cache reads still
require current enablement and a live ingestion-attempt fence. Cache retention is
tenant-scoped and follows tenant deletion; broader retention remains an owner decision.

## Acceptance contract

1. Digital pages are never submitted. Generated mixed PDFs preserve native pages.
2. OCR blocks carry an ocr origin, engine, nullable confidence and original page;
   rendered spans remain exact Unicode code-point slices. OCR citations say machine-read.
3. Disabled/unconfigured/budget-limited/uncertain/empty OCR remains typed needs_ocr
   and never publishes an empty or incomplete success. Errors contain no page text.
4. Add an ocr checkpoint after extraction to #726. Checkpoints include policy and
   provider identities, upstream checksum and canonical output; page cache survives
   downstream faults so completed pages resume without another network call.
5. Offline HTTP fixtures cover success/error annotations, invalid data, timeout,
   response limits, budget/tenant/concurrency denials and cache reuse. Fixtures are
   generated/documented schema examples plus one sanitized generated-page live
   recording. The recorded 429 chat response still returned parser annotations;
   exact separate file-envelope parts are removed inside the provider, leaving
   recognized code points unchanged. Missing usage retains the reserved ceiling.
6. Rust property tests verify exact offsets with supplementary Unicode/combining
   marks and preserve native controls. PDF/image preprocessing remains isolated.

## Alternatives and gates

#696 evaluates a self-hosted engine for offline deployment, boxes and language
coverage behind the same OcrProvider domain types. Handwriting remains out of scope.
No new service, vendor SDK or production parser promotion is introduced.
New direct Rust dependencies: image 0.25.8 (MIT OR Apache-2.0), with only
selected codecs, and tiff 0.10.3 (MIT) for rejecting extra frames.
`cargo deny check` passed on 2026-10-09 for the pinned graph (duplicate-version
warnings remain allowed by the existing policy). Image decode reserves 24 bytes
per pixel, four input copies and 4 MiB scratch before allocation; the isolated
worker additionally bounds non-cooperative codec/native work.

The optional live probe is not required: maximum two generated single-page requests
and $0.02 total if performed. Never retain keys or third-party document content.
Owner decisions: model/total ceilings and regional terms, tenant consent, cache
retention/reconciliation, held-out OCR quality thresholds and production rollout.
PDFium coverage/accounting calibration remains #739.

Merge gate: hold until measured against the baseline evaluation; a human merges.
