"""Token mint/verify — the only place a token is created or decoded.

Two token kinds (spec 0004 §2.3):

* **Access token** — a short-lived (<= 15 min), signed **JWT** with claims
  ``sub`` (user_id), ``tenant_id``, ``roles``, plus standard ``iss``/``iat``/
  ``exp``/``jti``. Stateless: verifying it needs only the secret, so every
  request validates without a DB hit. Decoding lives **only** here (ADR-0004) —
  no other module imports ``jwt``.
* **Refresh token** — an opaque, high-entropy random string handed to the
  client in an httpOnly cookie and stored **hashed** server-side (see
  ``db/`` ``RefreshTokenRepository``) so it is revocable and rotating. It is not
  a JWT and carries no claims; the server looks it up to refresh.

Verification failures (bad signature, expiry, wrong issuer, malformed) all raise
:class:`InvalidTokenError` → 401 (INV-4), never leaking which check failed.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.auth.errors import InvalidTokenError
from app.auth.principal import Principal
from app.core.config import Settings
from app.domain.entities import Role


@dataclass(frozen=True, slots=True)
class MintedAccessToken:
    """A freshly minted access token plus its lifetime (for ``expires_in``)."""

    token: str
    expires_in: int


def mint_access_token(principal: Principal, settings: Settings) -> MintedAccessToken:
    """Sign a short-lived access JWT for ``principal`` (spec 0004 §2.3 claims)."""
    now = datetime.now(UTC)
    ttl = settings.access_token_ttl_seconds
    claims: dict[str, object] = {
        "sub": str(principal.user_id),
        "tenant_id": str(principal.tenant_id),
        "roles": [r.value for r in principal.roles],
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return MintedAccessToken(token=token, expires_in=ttl)


def verify_access_token(token: str, settings: Settings) -> Principal:
    """Decode + validate an access JWT into a :class:`Principal`.

    Raises:
        InvalidTokenError: signature/issuer/expiry invalid, or the claim set is
            malformed (missing/!uuid ``sub``/``tenant_id``, bad ``roles``). All
            failures collapse to one 401 (INV-4) — no reason leaks to the client.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "tenant_id", "exp", "iss"]},
        )
    except jwt.InvalidTokenError as exc:  # covers expiry, signature, issuer, ...
        raise InvalidTokenError() from exc

    try:
        user_id = uuid.UUID(str(claims["sub"]))
        tenant_id = uuid.UUID(str(claims["tenant_id"]))
        raw_roles = claims.get("roles", [])
        if not isinstance(raw_roles, list):
            raise ValueError("roles claim is not a list")
        roles = tuple(Role(str(r)) for r in raw_roles)
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError() from exc

    return Principal(user_id=user_id, tenant_id=tenant_id, roles=roles)


# --- Refresh tokens (opaque; stored hashed) ---------------------------------

# Bytes of entropy for an opaque refresh token. 32 bytes = 256 bits, URL-safe.
_REFRESH_TOKEN_BYTES = 32


def generate_refresh_token() -> str:
    """Return a fresh, high-entropy, URL-safe opaque refresh token."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """Hash an opaque refresh token for at-rest storage / lookup.

    A refresh token is high-entropy random (not a low-entropy password), so a
    fast one-way SHA-256 is sufficient and deterministic — deterministic so the
    server can look the row up by hash. (Passwords use Argon2id; see
    ``hashing.py``.) The raw token is never persisted.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
