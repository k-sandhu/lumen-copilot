# 23. Direct media ingestion and transcription boundary

- **Status:** Accepted
- **Date:** 2026-08-11
- **Issue:** [#571](https://github.com/k-sandhu/lumen-copilot/issues/571)
- **Behavior:** [spec 0008](../specs/0008-direct-media-uploads-and-timestamp-citations.md)
- **Builds on:** [ADR-0004](0004-architecture-boundaries-and-adapters.md),
  [ADR-0006](0006-contract-first-parallel-implementation.md),
  [ADR-0010](0010-dedicated-text-search-engine.md)
- **Amends:** [ADR-0003 §7](0003-application-stack.md#7-llm-access--litellm-gateway-openrouter-first)

## Context

The original upload API accepts multipart form data, reads the whole file in
FastAPI, then writes it to MinIO. That path duplicates large bytes, consumes API
memory/bandwidth, cannot resume, and makes media impractical. The viewer likewise
buffers complete objects because protected media elements cannot attach the Lumen
bearer token to a redirect.

Media also needs a durable time axis and stable diarized turns. Character-only
chunks cannot support a verifiable seek target. Video must keep its original
object while transcription operates only on extracted audio.

OpenRouter currently offers `x-ai/grok-stt-1.0`, whose published capability set
includes word timestamps and optional speaker diarization. LiteLLM 1.55.9, pinned
by this repository, only maps transcription for OpenAI, Azure, and Groq; the
current LiteLLM provider matrix also does not advertise OpenRouter transcription.
Routing OpenRouter through the OpenAI compatibility shim does not expose the
provider-specific diarization options/word response, which would silently defeat
the feature.

## Decision

### 1. Storage data plane

FastAPI is the authenticated **control plane** only. S3-compatible multipart PUTs
and signed Range GETs are the **data plane** and flow directly between browser and
object storage. The browser receives only short-lived, least-privilege URLs for
expected part numbers or one visible object. Raw provider upload ids and storage
keys remain server-side.

The new control plane is a versioned `/api/v2` surface. The same release migrates the
SPA and retires the unsafe v1 byte operations with authenticated `410` responses.
This is the coordinated breaking migration allowed by the contracts rule; the old
upload cannot remain functional behind a flag because its very availability would
violate the “no FastAPI file bytes” invariant.

Uploads use random tenant-prefixed quarantine keys instead of the former
pre-upload content hash. Client-declared hashes are not trusted as canonical
keys. A document row is created only after multipart completion and `HEAD`
verification. A tenant-scoped upload-session state machine makes retries,
recovery, expiry, and exact-once audit/enqueue explicit.

### 2. Media worker pipeline

Celery owns all blocking/CPU/provider work. It streams the object to a temporary
file, validates with ffprobe, extracts normalized audio from video with FFmpeg,
transcribes bounded chunks, persists transcript results, then builds retrieval
chunks and synchronizes OpenSearch. The original video stays immutable and is the
viewer/citation time reference. Retry resumes from persisted transcript chunks so
provider charges are not duplicated.

Transcript segments are normalized relational rows. Retrieval chunks carry their
covering millisecond span. PostgreSQL remains authoritative; OpenSearch carries
the same optional fields under its strict mapping only to retrieve candidates.

### 3. Model boundary and narrow LiteLLM exception

`backend/app/llm/` remains the single owning module for every model provider.
Chat, embeddings, tools, and contextual name reasoning continue through
`LLMGateway` and LiteLLM.

For **OpenRouter speech-to-text only**, that module contains a small HTTP adapter
for `POST /api/v1/audio/transcriptions`. This is a deliberate, narrow exception to
the “every model call through LiteLLM” transport rule because LiteLLM does not
support the required OpenRouter endpoint/fields. It is not permission to call a
provider from services, tasks, or any other module. The adapter exposes only
provider-neutral transcription domain types, uses the existing OpenRouter secret
and configured model/base URL, applies typed error mapping/timeouts, validates all
timestamps/speaker labels, and has contract fixtures. The provider-specific
passthrough payload is configuration plus an opt-in live conformance test; the
offline default is never treated as proven merely because OpenRouter advertises
the capability. A response without the required words/speakers fails closed.
Once LiteLLM demonstrably
round-trips OpenRouter diarization and word timestamps, the adapter is replaced by
LiteLLM without changing callers.

This exception preserves the more important architecture invariant—the provider
has one chokepoint and vendor types never escape—while avoiding fabricated or
dropped citation evidence.

This paragraph explicitly amends ADR-0003 §7's transport statement. It does not
edit the root agent contract: the session's requested OpenRouter transcription
capability is implemented under the higher-precedence accepted ADR while the
exception remains narrow and mechanically guarded by an import-boundary test.

### 4. Speaker identity

Diarizer ids are stable only within a document. Contextual display names are
derived from explicit transcript evidence and always retain provenance and an
“inferred” marker. No voice embeddings, biometric matching, or authorization
decision uses a name.

### 5. Access and revocation

The API returns a JSON signed-access capability after rechecking visibility and
auditing. Media elements then talk directly to storage and can issue Range
requests. The accepted revocation window is the short URL TTL: revoking access
prevents new capabilities immediately, while an already minted URL remains usable
until expiry. URLs are never logged or persisted.

## Consequences

- API memory and bandwidth no longer scale with object size; transfers can resume
  by part and native playback can seek efficiently.
- Storage CORS and incomplete-multipart cleanup become required deployment
  mechanisms. Community MinIO's CORS allow-list is process-level and its S3
  compatibility layer does not implement `AbortIncompleteMultipartUpload`;
  Compose therefore explicitly marks both controls externally managed and runs
  a pinned `mc` reaper for incomplete uploads older than the configured limit.
  S3 providers install bucket CORS and merge the named lifecycle rule instead.
- Multipart completion crosses storage and database transaction boundaries, so a
  `completing` state plus janitor recovery is necessary.
- Quarantined random keys trade pre-upload deduplication for safe direct transfer.
- FFmpeg/ffprobe become pinned worker runtime dependencies and media concurrency
  must be bounded separately from ordinary document ingestion.
- Timestamp provenance becomes a cross-cutting nullable field through database,
  OpenSearch, retrieval, chat, runs, REST, WebSocket, and UI.
- The one direct provider HTTP path is intentionally easy to find and delete. It
  may not grow to chat, embeddings, or another provider without a new ADR.

## Rejected alternatives

- **Keep proxy uploads/downloads:** fails the large-file, resume, memory, and Range
  requirements.
- **One presigned PUT per file:** direct, but large failures restart from byte zero
  and cancellation/resume state is opaque.
- **Trust a browser SHA-256 for the old key scheme:** requires a whole-file pass and
  permits a malicious declaration to target an existing key.
- **Run transcription in FastAPI:** provider latency/decoding is slow and blocking;
  retries would wedge request workers.
- **Use LiteLLM's OpenAI transcription compatibility route today:** it does not
  preserve OpenRouter's diarization contract, so a “successful” call could produce
  unverifiable speaker/timestamp data.
- **Infer identity from voice:** biometric scope and risk are unnecessary; meeting
  dialogue already provides auditable contextual evidence.
