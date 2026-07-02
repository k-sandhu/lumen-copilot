"""Object-key construction — pure, I/O-free helpers (ADR-0004 boundary).

The storage adapter addresses every object by a **tenant-prefixed,
content-addressed** key:

    {tenant_id}/{sha256(bytes)}/{safe_filename}

* The leading ``tenant_id`` segment is the **isolation seam** (issue #22 AC-2):
  the adapter refuses a ``get``/``presign_get`` whose key does not start with the
  caller's tenant prefix, so cross-tenant reads are impossible from day one —
  before the full ACL lands in CC-1. Real tenant *resolution* is CC-2/CC-3; here
  the caller supplies ``tenant_id``.
* The ``sha256`` middle segment makes the key **content-addressed**: identical
  bytes map to the same key, so re-uploading a file dedupes for free.

These functions are deliberately pure (no S3, no settings) so the key invariants
are unit-testable with zero mocks; the adapter in :mod:`app.storage.object_store`
composes them.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import unicodedata

from app.core.errors import ForbiddenError, ValidationError

# A conservative allowlist for the safe-filename slug: ASCII letters, digits, and
# a small set of separators. Everything else is collapsed to ``_`` so the key can
# never contain path traversal (``..``, ``/``) or control characters.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_FILENAME_LEN = 200
_FALLBACK_FILENAME = "file"


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 of ``data`` (the content address)."""
    return hashlib.sha256(data).hexdigest()


def validate_tenant_id(tenant_id: str) -> str:
    """Return ``tenant_id`` if it is a single safe key segment, else reject.

    A tenant id that contained ``/`` or ``..`` would let a caller forge a prefix
    or escape its own namespace, defeating the isolation seam — so it is rejected
    with a typed :class:`ValidationError` rather than silently sanitized.
    """
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValidationError(
            "tenant_id must be a single safe path segment (letters, digits, '.', '_', '-')",
            code="invalid_tenant_id",
        )
    return tenant_id


def safe_filename(filename: str) -> str:
    """Reduce ``filename`` to a single safe key segment.

    Strips any directory components, normalizes unicode, replaces unsafe runs
    with ``_``, trims leading dots (no hidden/relative names), and caps length.
    Always returns a non-empty value so the key's final segment is stable.
    """
    # Drop any path the client may have sent; keep only the base name.
    base = posixpath.basename(filename.replace("\\", "/")).strip()
    # Normalize unicode to its compatibility form, then drop non-encodable chars.
    base = unicodedata.normalize("NFKC", base)
    base = base.encode("ascii", "ignore").decode("ascii")
    # Collapse unsafe character runs to a single underscore.
    cleaned = _SAFE_FILENAME_RE.sub("_", base)
    # No leading dots (would create hidden or relative-looking names).
    cleaned = cleaned.lstrip(".")
    cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned or _FALLBACK_FILENAME


def build_key(tenant_id: str, data: bytes, filename: str) -> str:
    """Assemble the content-addressed, tenant-prefixed key for ``data``.

    Shape: ``{tenant_id}/{sha256(data)}/{safe_filename}`` (AC-2).
    """
    return f"{validate_tenant_id(tenant_id)}/{sha256_hex(data)}/{safe_filename(filename)}"


# Namespace prefix for agent/run-produced artifacts (issue #208), kept distinct
# from uploaded documents so the two never share a key even for identical bytes,
# and so a bucket lifecycle rule / listing can target artifacts alone. The
# ``assert_key_owned_by`` tenant-prefix seam is applied to the *tenant segment*,
# which sits **after** this constant prefix — see :func:`assert_artifact_key_owned_by`.
_ARTIFACT_PREFIX = "artifacts"


def build_artifact_key(tenant_id: str, data: bytes, filename: str) -> str:
    """Assemble the artifact object key (issue #208, AC-5).

    Shape: ``artifacts/{tenant_id}/{sha256(data)}/{safe_filename}`` — the same
    tenant-prefixed, content-addressed construction as :func:`build_key`, under
    the dedicated ``artifacts/`` namespace so agent output never collides with an
    uploaded document. Reuses :func:`validate_tenant_id` (a forged/traversing
    tenant id is refused) and :func:`safe_filename` (no path traversal in the
    final segment).
    """
    return (
        f"{_ARTIFACT_PREFIX}/{validate_tenant_id(tenant_id)}/"
        f"{sha256_hex(data)}/{safe_filename(filename)}"
    )


def assert_key_owned_by(key: str, tenant_id: str) -> None:
    """Enforce the tenant-prefix isolation seam (AC-5).

    Raise :class:`ForbiddenError` if ``key`` does not live under the caller's
    ``{tenant_id}/`` prefix. This is the day-one cross-tenant refusal at the
    adapter, ahead of the full ACL in CC-1. Refusing with 403 (not 404) is the
    correct existence-blind tenant boundary — the caller is told "not yours",
    not whether the object exists in another tenant.
    """
    prefix = f"{validate_tenant_id(tenant_id)}/"
    if not key.startswith(prefix):
        raise ForbiddenError(
            "object key is outside the caller's tenant prefix",
            code="cross_tenant_denied",
        )


def assert_artifact_key_owned_by(key: str, tenant_id: str) -> None:
    """Enforce the tenant-prefix isolation seam for an **artifact** key (#208 AC-5).

    Artifact keys live under ``artifacts/{tenant_id}/…`` (:func:`build_artifact_key`),
    so the tenant segment is the *second* path component, not the first. Raise
    :class:`ForbiddenError` unless ``key`` starts with ``artifacts/{tenant_id}/``.
    This blocks a forged cross-tenant artifact key before any read/delete/presign,
    exactly as :func:`assert_key_owned_by` does for uploads — and it also refuses a
    key missing the ``artifacts/`` prefix (e.g. an upload key smuggled in), so the
    two namespaces can never be confused. Refusing with 403 (not 404) is the
    existence-blind tenant boundary at the adapter.
    """
    prefix = f"{_ARTIFACT_PREFIX}/{validate_tenant_id(tenant_id)}/"
    if not key.startswith(prefix):
        raise ForbiddenError(
            "artifact key is outside the caller's tenant prefix",
            code="cross_tenant_denied",
        )
