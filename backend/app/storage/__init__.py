"""Object storage adapter (S3 / MinIO) — the only S3 client owner.

Single responsibility (ADR-0004 boundary table): own all object-store access
for uploads and derived artifacts. **Nobody else may construct an S3/MinIO
client.** Locally this points at MinIO; production swaps the endpoint with no
application change.

Public surface (issue #22): ``ObjectStore`` exposes ``put``/``get``/``delete``/
``presign_put``/``presign_get`` over tenant-prefixed keys with content-type +
size validation; direct multipart methods use random quarantine keys so file
bytes remain outside FastAPI (spec 0008). ``StoredObject`` is the small-object
descriptor. It also exposes the parallel
``put_artifact``/``get_artifact``/``delete_artifact``/
``presign_get_artifact`` lifecycle for agent/run-produced artifacts (issue #208)
under an ``artifacts/`` key namespace with the broader artifact allowlist. DLP,
KMS/SSE, and the parsing sandbox are fenced OUT to OD-4 / CC-5 #21.
"""

from app.storage.object_store import (
    MultipartUpload,
    ObjectStore,
    StoredObject,
    StoredObjectMetadata,
    UploadedPart,
)

__all__ = [
    "MultipartUpload",
    "ObjectStore",
    "StoredObject",
    "StoredObjectMetadata",
    "UploadedPart",
]
