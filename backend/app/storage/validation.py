"""Upload validation — pure, I/O-free checks (issue #22 AC-4 / AC-6).

The declared content-type and the byte size are validated against the
config-driven allowlist/limit **before** anything is stored. A rejection is a
typed :class:`ValidationError` (rendered as a 4xx problem+json by the API's
exception handler) — never a silent drop and never a 500 (AC-6).

Scope note: a *client-declared* content-type is a usability/allowlist check, not
a security guarantee. Content sniffing, malware/DLP scanning, and the
text-extraction parsing sandbox are explicitly fenced **OUT** of #22 (CC-5 #21,
OD-4 data invariants); this function only enforces the allowlist + size limit.
"""

from __future__ import annotations

from collections.abc import Collection

from app.core.errors import ValidationError
from app.domain.entities import DocumentKind

AUDIO_CONTENT_TYPES = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp4",
        "audio/aac",
        "audio/flac",
        "audio/ogg",
        "audio/webm",
    }
)
VIDEO_CONTENT_TYPES = frozenset({"video/mp4", "video/webm"})

# Filename suffixes are an early control-plane consistency check, not a claim
# about the object's bytes. Container/parser validation still happens in the
# quarantined ingestion worker. Ambiguous containers deliberately admit both
# accepted families; a generic browser declaration uses the first canonical
# type, matching the frontend's safe inference.
_EXTENSION_CONTENT_TYPES: dict[str, tuple[str, ...]] = {
    "pdf": ("application/pdf",),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
    "txt": ("text/plain",),
    "md": ("text/markdown",),
    "wav": ("audio/wav",),
    "mp3": ("audio/mpeg",),
    "m4a": ("audio/mp4",),
    "aac": ("audio/aac",),
    "flac": ("audio/flac",),
    "ogg": ("audio/ogg",),
    "mp4": ("video/mp4", "audio/mp4"),
    "webm": ("video/webm", "audio/webm"),
}

# Browser/platform aliases are accepted only with the suffix that disambiguates
# them, then collapsed to the canonical type persisted in the upload session.
_EXTENSION_MIME_ALIASES: dict[str, dict[str, str]] = {
    "m4a": {"audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4"},
    "mp3": {"audio/mp3": "audio/mpeg"},
    "wav": {
        "audio/vnd.wave": "audio/wav",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
    },
}


def normalize_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def canonical_content_type_for_filename(filename: str, content_type: str) -> str:
    """Return a suffix-compatible canonical declaration or reject it early.

    An unknown/missing suffix is unsupported (415 after service mapping). A
    known suffix paired with a different otherwise plausible MIME declaration
    is malformed metadata (422). ``application/octet-stream`` is accepted only
    when a known suffix supplies the missing type, and known browser aliases are
    similarly scoped to their matching suffix. This prevents an allowlisted MIME
    from disguising an executable/unknown filename while preserving safe browser
    interoperability.
    """
    stem, separator, extension = filename.rpartition(".")
    normalized_extension = extension.lower()
    expected = _EXTENSION_CONTENT_TYPES.get(normalized_extension)
    if not separator or not stem or expected is None:
        raise ValidationError(
            "filename extension is not supported",
            code="filename_extension_not_allowed",
        )

    declared = normalize_content_type(content_type)
    if declared == "application/octet-stream":
        return expected[0]
    if declared in expected:
        return declared
    alias = _EXTENSION_MIME_ALIASES.get(normalized_extension, {}).get(declared)
    if alias is not None:
        return alias
    raise ValidationError(
        "filename extension does not match the declared content type",
        code="filename_content_type_mismatch",
    )


def document_kind_for_content_type(content_type: str) -> DocumentKind:
    """Classify an allowlisted declaration into its processing/viewer family."""
    declared = normalize_content_type(content_type)
    if declared in AUDIO_CONTENT_TYPES:
        return DocumentKind.AUDIO
    if declared in VIDEO_CONTENT_TYPES:
        return DocumentKind.VIDEO
    return DocumentKind.DOCUMENT


def validate_upload(
    *,
    size_bytes: int,
    content_type: str,
    allowed_content_types: Collection[str],
    max_bytes: int,
) -> None:
    """Reject a disallowed content-type or an over-limit / empty payload.

    Raises:
        ValidationError: the declared type is not allowlisted, the payload is
            empty, or it exceeds ``max_bytes``. The error carries a stable
            machine-readable ``code`` so the API can map it precisely.
    """
    # Normalize: a declared type may carry parameters, e.g. "text/plain; charset=utf-8".
    declared = normalize_content_type(content_type)
    if declared not in {ct.lower() for ct in allowed_content_types}:
        raise ValidationError(
            f"content-type {content_type!r} is not allowed",
            code="content_type_not_allowed",
        )

    if size_bytes <= 0:
        raise ValidationError("upload is empty", code="empty_upload")

    if size_bytes > max_bytes:
        raise ValidationError(
            f"upload is {size_bytes} bytes; limit is {max_bytes} bytes",
            code="upload_too_large",
        )
