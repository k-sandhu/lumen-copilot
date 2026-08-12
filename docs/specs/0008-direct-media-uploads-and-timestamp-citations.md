# Spec 0008 — Direct media uploads and timestamp citations

- **Status:** Accepted
- **Date:** 2026-08-11
- **Issue:** [#571](https://github.com/k-sandhu/lumen-copilot/issues/571)
- **Architecture:** [ADR-0023](../architecture/0023-direct-media-ingestion.md)

## 1. Capability

Lumen accepts documents, audio, and video without proxying file bytes through
FastAPI. The browser uploads bounded parts directly to object storage, while the
backend remains the authenticated control plane for capabilities, completion,
permissions, and audit.

Audio is transcribed into ordered, diarized speaker turns. Video keeps its
original object for playback and reference while a worker extracts normalized
audio for transcription. Retrieval chunks and citations preserve player-relative
timestamps so every cited media claim can seek to what was said.

Speaker names are **contextual inferences**, never biometric identities. Stable
file-local diarizer labels remain authoritative. A display name is attached only
when explicit transcript evidence supports it (for example, a self-introduction
or an unambiguous direct-address/response exchange); otherwise the UI says
“Speaker 1”, “Speaker 2”, and so on and labels inferred names as inferred.

## 2. Direct multipart upload behavior

1. The authenticated browser initiates an upload at `POST /api/v2/document-uploads`
   with JSON metadata only:
   filename, declared MIME type, byte size, collection, and optional client
   last-modified time. File bytes are not accepted by a Lumen API route.
2. The backend verifies collection ownership, allowed type, size, and expected
   part count before creating a tenant/owner-scoped upload session and a random
   tenant-prefixed quarantine key.
3. The browser requests short-lived URLs for a small batch of expected part
   numbers and uploads `File.slice()` bodies directly to storage. Storage
   requests carry neither the Lumen bearer token nor cookies.
4. The browser uses bounded concurrency (three files, four parts per file),
   exponential retry with jitter, aggregate byte progress, cancel/abort, and
   resume from the server-reported completed-part list. An expired part URL is
   re-signed rather than restarting the file.
5. Completion is idempotent. The backend locks the session, completes the
   provider multipart upload, verifies stored size and metadata with `HEAD`,
   creates exactly one pending `Document`, emits `document.uploaded` exactly
   once, commits, and only then enqueues ingestion.
6. Abort is idempotent before completion. Expired sessions are failed closed and
   a janitor aborts abandoned provider uploads. A completed session cannot be
   aborted (`409`). Cross-tenant or non-owned sessions are hidden as `404`.
7. The replacement upload/playback/transcript surface is versioned under `/api/v2`.
   The former multipart `POST /documents` upload is disabled with an authenticated
   `410` in the coordinated frontend/backend migration, so there is no supported
   file-body path through FastAPI.

Defaults are configuration, not literals: 8 MiB parts, at most 10,000 parts,
24-hour sessions, 15-minute part URLs, 50 MiB ordinary-document limit, and 5 GiB
media limit. Empty files, impossible part counts, disallowed types, declared or
stored oversize objects, size mismatches, invalid ETags, expired sessions, and
illegal state transitions fail with the shared problem response.

## 3. Accepted media and processing

The initial media allowlist is deliberately browser-playable and model-friendly:

- Audio: WAV, MP3, MP4/M4A, AAC, FLAC, Ogg, and WebM.
- Video: MP4 and WebM.

MIME declarations are only an early rejection. Until ingestion validates the
container and streams with `ffprobe`, the object remains quarantined and cannot
be retrieved or cited. The media duration default limit is eight hours.

Workers download originals to bounded temporary files rather than loading the
whole object into memory. For video, FFmpeg extracts mono 16 kHz audio with a
zero-based presentation timeline; the original video object is never replaced.
The derived audio is transient. Long media is split into overlapping chunks no
longer than ten minutes so provider timeout/retry never rebills already persisted
chunks. Paid transcription results are persisted before embedding/indexing and
are reused after retry.

The configured default transcription route is OpenRouter model
`x-ai/grok-stt-1.0`, requested with word timestamps and speaker diarization via
the adapter's configured provider passthrough. An opt-in live conformance test
must prove that the deployed OpenRouter route returns both before it is marked
healthy; published capability metadata alone is insufficient.
Provider responses missing ordered words, valid non-negative time spans, or
speaker labels fail ingestion; Lumen never fabricates timing or diarization.

## 4. Transcript and identity model

Each ready media document has:

- a zero-based duration in integer milliseconds;
- detected language and transcription model provenance;
- stable speakers identified by file-local ids such as `speaker-1`;
- ordered transcript segments with half-open `[start_ms, end_ms)` spans, speaker
  id, text, and optional confidence;
- optional inferred speaker display name, confidence tier, inference method, and
  evidence segment ids.

Name inference may use only the transcript for that document. It must validate
that every evidence id exists and every returned speaker id was produced by the
diarizer. Conflicts or ambiguity remove the display name. Names are presentation
metadata only: they never grant permission, link voices across files, or create a
voiceprint.

## 5. Timestamped citations

Media transcript chunks carry nullable `time_start_ms`, `time_end_ms`, and source
segment bounds in addition to canonical character offsets. Search indexing,
Postgres hydration, grounded chat, saved messages, scheduled runs, REST, and the
WebSocket citation event preserve these values end to end.

For media, timestamp fields are a pair and satisfy
`0 <= time_start_ms < time_end_ms <= duration_ms`. A citation with missing,
reversed, out-of-range, cross-document, or unauthorized timing is blocked under
INV-3. Ordinary document citations keep both fields null. Permission revocation
redacts media timestamps with the same shell-preserving behavior as text snippets.

## 6. Viewer and access URLs

The authenticated API mints a short-lived JSON access capability for preview or
download only after the normal visibility check and audit. The response contains
the signed URL, MIME type, and expiry; the URL and provider upload ids never enter
logs or audit metadata. Native `<audio>` and `<video>` elements use that URL with
`preload="metadata"`, letting Range requests flow browser ↔ storage.

The shared viewer presents the native player above an independently scrollable,
paginated transcript. A transcript timestamp or citation seeks after metadata is
loaded without autoplay. The active segment is visually highlighted. A URL that
expires during playback is refreshed once for that failed capability while
preserving current time and play/pause state; a later independently expired
capability may be renewed in the same way. Codec failures never create a refresh
loop. Loading, processing, empty, permission-revoked, transient-error, and retry
states are explicit and keyboard accessible.

## 7. Security, audit, and negative acceptance

- Upload sessions, transcripts, and capabilities are tenant and owner/permission
  scoped; wrong tenant or forbidden direct read is `404` (INV-1/INV-2).
- Missing/expired authentication is `401` (INV-4).
- Upload completion is a T1 write; no T2+ action is introduced.
- Initiation, completion, abort, expiry, transcription, transcript view, and
  access-capability minting emit an event through the one audit sink. Capabilities,
  storage keys, and provider response bodies are not audit metadata (INV-6).
- Malformed metadata/parts/timestamps are `422`; illegal transitions are `409`
  (INV-8). Unsupported type is `415`; declared/stored oversize is `413`.
- Storage CORS permits only configured frontend origins and methods
  `PUT/GET/HEAD`; it exposes `ETag`, `Content-Length`, `Content-Range`, and
  `Accept-Ranges`. Browser data-plane requests never send Lumen bearer tokens,
  cookies, or credential mode. S3-compatible deployments install bucket CORS;
  community MinIO receives the same allow-list through its process-level CORS
  setting because that distribution does not implement `PutBucketCors`.
- Incomplete multipart uploads have an independent provider-side backstop. S3
  deployments merge a named `AbortIncompleteMultipartUpload` lifecycle rule;
  community MinIO Compose runs a pinned `mc rm --incomplete --older-than` reaper
  because that lifecycle action is not implemented by its S3 API. The ordinary
  database janitor remains responsible for known upload-session state.

## 8. Scope fence

In scope: uploaded files, resumable multipart transfer, original media playback,
video audio extraction, batch transcription, diarization, contextual name
inference, transcripts, and timestamp citations.

Out of scope: live recording/transcription, video-frame understanding, subtitles
burned into video, translation, biometric/cross-file voice recognition, manual
speaker-name editing, upload continuation across browser restarts, tenant storage
quotas/billing, and playback transcoding beyond the accepted formats.

## 9. Verification

Automated tests must prove that FastAPI never receives a successful file-body
upload, direct part requests omit Lumen credentials, concurrency is bounded,
retry/resume/cancel are correct, completion is idempotent and audited, media
timestamps survive retrieval/citation persistence, invalid citations are blocked,
and citation activation seeks the audio/video player. A live MinIO test covers
multipart CORS, exposed ETags, and byte ranges; a synthetic media fixture covers
FFmpeg extraction and timeline alignment. A live OpenRouter test is opt-in because
it incurs cost and requires a configured key.
