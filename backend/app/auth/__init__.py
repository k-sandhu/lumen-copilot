"""Identity & tenant resolution — the only token validator (ADR-0004).

Single responsibility (boundary table): answer "who is asking" and "which
tenant" for every request, and own password hashing + token mint/verify.
**Nobody else may resolve a user/tenant, validate a token, or hash a password.**
The resolved :class:`Principal` keys the ``db/`` repositories and the
``retrieval/`` permission filter (mission filter #1, permissioned by default).

MVP is app-managed email+password → short-lived access JWT + rotating refresh
token (spec 0004 §2.3). The OIDC/Keycloak end-state swaps the token source
behind this same surface (the adapter rule); callers never change.
"""

from __future__ import annotations

from app.auth.errors import InvalidCredentialsError, InvalidTokenError, RefreshSupersededError
from app.auth.hashing import (
    dummy_verify,
    dummy_verify_async,
    hash_password,
    verify_password,
    verify_password_async,
)
from app.auth.principal import Principal
from app.auth.tokens import (
    MintedAccessToken,
    generate_refresh_token,
    hash_refresh_token,
    mint_access_token,
    verify_access_token,
)

__all__ = [
    "InvalidCredentialsError",
    "InvalidTokenError",
    "RefreshSupersededError",
    "MintedAccessToken",
    "Principal",
    "dummy_verify",
    "dummy_verify_async",
    "generate_refresh_token",
    "hash_password",
    "hash_refresh_token",
    "mint_access_token",
    "verify_access_token",
    "verify_password",
    "verify_password_async",
]
