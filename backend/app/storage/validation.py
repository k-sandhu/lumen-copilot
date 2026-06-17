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
    declared = content_type.split(";", 1)[0].strip().lower()
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
