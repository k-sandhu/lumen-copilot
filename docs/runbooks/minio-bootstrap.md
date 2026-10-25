# MinIO live bootstrap and browser CORS

The MinIO integration fixture follows [ADR-0023](../architecture/0023-direct-media-ingestion.md):
Community MinIO owns CORS at process level and incomplete multipart cleanup is
externally managed. The fixture defaults `S3_CORS_MANAGED_EXTERNALLY` and
`S3_INCOMPLETE_MULTIPART_CLEANUP_MANAGED_EXTERNALLY` to `true`, matching Compose.
It reads `S3_CORS_ALLOWED_ORIGINS` (default `http://localhost:47180`) so HTTP checks
use the configured origin. Explicit environment flags take precedence, including
`false`; inspect inherited shell values before running live tests.

General S3 `Settings` still default both flags to `false`: bootstrap installs
bucket CORS and merges the named lifecycle rule. There is no provider detection
or fallback after `NotImplemented`. Failed bucket access or creation still raises
safe `storage_bootstrap_failed` and leaves readiness incomplete. An external flag
asserts deployment ownership; it does not configure or verify the external control.

## Local setup

Compose configures MinIO's `MINIO_API_CORS_ALLOW_ORIGIN` from the same
`S3_CORS_ALLOWED_ORIGINS` allow-list used by the backend. Set that process variable
before starting standalone MinIO too. Restart MinIO after changing it. Origins
include scheme, host and port; `localhost` and `127.0.0.1` are different origins.

From `backend/`, run `uv run --extra dev pytest tests/test_object_store.py -v`.
Offline runs exercise fixture/default and safe failure regressions. To opt into
live tests, set `RUN_LIVE=1`, `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL`, the local
credentials and a unique `S3_BUCKET`. Set `S3_CORS_ALLOWED_ORIGINS` to the same
explicit origin allow-list supplied to the MinIO process. Explicitly select both
external-management flags when the calling shell has other S3 settings.

A standalone short test can explicitly abort its incomplete uploads; persistent
deployments require the external reaper. Its repaired Compose shell invocation
keeps all cleanup flags and the bucket in one command; see the
[multipart reaper runbook](multipart-reaper.md) for verification (#804).
Do not use wildcard CORS to fix a test.

## Browser verification and teardown

HTTP preflight assertions exercise the provider but do not prove browser enforcement.
Serve a temporary page from an allowlisted origin and mint short-lived signed URLs
with `ObjectStore` for unique tenant-prefixed keys. In a real browser, PUT bytes
with `fetch`, require success and readable `ETag`, then GET and compare exact
bytes. For multipart/media, also check signed Range GET and readable `Content-Range`.

Serve the page from a different, non-allowlisted origin. Require browser `fetch`
to reject the signed PUT preflight and cross-origin GET. Verify through the adapter
that denied PUT created no object. CORS controls browser access; a signed URL
remains a bearer capability until expiry.

Delete temporary objects, abort unfinished multipart uploads, verify absence and
remove only the temporary bucket. Stop the page servers/browser and tear down only
the test project's MinIO container and volume. The integration tests compare bytes,
check tenant refusal and signed multipart/Range headers, and verify object absence
in teardown; the caller owns removal of a deliberately provisioned test bucket.
