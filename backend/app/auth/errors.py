"""Auth-specific typed errors (subtypes of the core error model).

These render as ``problem+json`` via the same exception handler as every other
``AppError`` (app.core.errors). They keep messages **generic** — a failed login
and an unknown account are the same 401 with no account-existence disclosure
(spec 0004 §2.3); an expired token and a forged token are the same 401 (INV-4).
The distinct ``code`` lets clients branch (e.g. refresh-on-expiry) without the
human-readable title leaking the reason.
"""

from __future__ import annotations

from app.core.errors import UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    """Wrong email/password — generic, no account-existence disclosure."""

    code = "invalid_credentials"
    title = "Unauthorized"

    def __init__(self) -> None:
        # Fixed, generic detail: identical whether the email exists or not.
        super().__init__("Invalid email or password.")


class InvalidTokenError(UnauthorizedError):
    """A bearer/refresh token is missing, malformed, expired, or revoked (INV-4)."""

    code = "invalid_token"
    title = "Unauthorized"

    def __init__(self, detail: str = "Not authenticated.") -> None:
        super().__init__(detail)
