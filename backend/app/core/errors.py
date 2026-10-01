"""Typed application errors + the RFC-9457 problem-detail mapper.

One error model for the whole backend (backend/AGENTS.md "Errors"): domain and
service code raise a typed :class:`AppError`; a single FastAPI exception handler
(registered in ``app.main``) maps it to ``application/problem+json`` matching the
``Problem`` schema in ``contracts/openapi.yaml``. Stack traces and vendor errors
are never leaked to the client.

The ``Problem`` Pydantic model here is the in-code mirror of that contract
shape; the WS error envelope reuses the same fields (see
``contracts/websocket-envelopes.schema.json`` ``Error.problem``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

PROBLEM_CONTENT_TYPE = "application/problem+json"


class FieldError(BaseModel):
    """A single field-level validation error (``Problem.errors[]``)."""

    field: str
    message: str


class Problem(BaseModel):
    """RFC-9457 problem detail — the one error shape for all responses.

    Mirrors ``#/components/schemas/Problem`` in ``contracts/openapi.yaml``. The
    same field set backs the WebSocket ``error`` envelope's ``problem`` object.
    """

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str | None = None
    errors: list[FieldError] | None = None


class AppError(Exception):
    """Base class for all typed application errors.

    Subclasses set a default HTTP ``status``, a stable machine-readable
    ``code``, and a human ``title``. Services and domain policy raise these;
    the exception handler renders them as ``Problem`` payloads. Raising an
    ``AppError`` (rather than an ``HTTPException`` or a bare ``Exception``)
    keeps the error model in one place and out of the routers.
    """

    status: int = 500
    code: str = "internal_error"
    title: str = "Internal Server Error"
    problem_type: str = "about:blank"

    def __init__(
        self,
        detail: str | None = None,
        *,
        title: str | None = None,
        code: str | None = None,
        status: int | None = None,
        field_errors: list[FieldError] | None = None,
    ) -> None:
        self.detail = detail
        if title is not None:
            self.title = title
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.field_errors = field_errors
        super().__init__(detail or self.title)

    def to_problem(self, *, instance: str | None = None) -> Problem:
        """Render this error as a :class:`Problem`."""
        return Problem(
            type=self.problem_type,
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            code=self.code,
            errors=self.field_errors,
        )


# --- Concrete error types (the seams; feature code raises these) -------------


class NotFoundError(AppError):
    """A requested resource does not exist (or is not visible to the caller)."""

    status = 404
    code = "not_found"
    title = "Not Found"


class ValidationError(AppError):
    """The request was syntactically valid but semantically rejected."""

    status = 422
    code = "validation_error"
    title = "Unprocessable Entity"


class ConflictError(AppError):
    """The request conflicts with the current resource state (illegal transition, INV-8).

    Mapped to **409** (the contract's ``Conflict`` response): e.g. ``run-now`` on a
    paused schedule, or acting on a resource in a state that forbids the action.
    Distinct from :class:`ValidationError` (422, malformed input) — a 409 is a valid
    request against a resource whose state rejects it.
    """

    status = 409
    code = "conflict"
    title = "Conflict"


class UnauthorizedError(AppError):
    """The caller is not authenticated."""

    status = 401
    code = "unauthorized"
    title = "Unauthorized"


class ForbiddenError(AppError):
    """The caller is authenticated but not permitted (permission/tenant)."""

    status = 403
    code = "forbidden"
    title = "Forbidden"


class GoneError(AppError):
    """A deliberately retired resource/operation (contract 410)."""

    status = 410
    code = "gone"
    title = "Gone"


class PayloadTooLargeError(AppError):
    """An upload exceeds the configured size cap (contract 413)."""

    status = 413
    code = "payload_too_large"
    title = "Payload Too Large"


class UnsupportedMediaTypeError(AppError):
    """A declared content-type is not in the upload allowlist (contract 415)."""

    status = 415
    code = "unsupported_media_type"
    title = "Unsupported Media Type"


class DependencyError(AppError):
    """A downstream dependency is unavailable (mapped to 503)."""

    status = 503
    code = "dependency_unavailable"
    title = "Service Unavailable"


def problem_from_exception(
    exc: Exception, *, instance: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Map any exception to ``(status_code, problem_dict)``.

    Known :class:`AppError`s render their declared shape. Anything else is
    coerced to an opaque 500 — never leaking the original message, stack trace,
    or vendor error to the client (backend/AGENTS.md "Errors").

    Returns:
        A ``(status, body)`` tuple; ``body`` is the JSON-able ``Problem`` dict
        with ``None`` fields omitted.
    """
    if isinstance(exc, AppError):
        problem = exc.to_problem(instance=instance)
        return problem.status, problem.model_dump(exclude_none=True)

    generic = Problem(
        title="Internal Server Error",
        status=500,
        code="internal_error",
        instance=instance,
    )
    return generic.status, generic.model_dump(exclude_none=True)
