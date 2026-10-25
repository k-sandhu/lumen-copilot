"""Opaque cache scopes; provider payloads remain gateway-owned (ADR-0016)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from uuid import UUID


def cache_scopes(secret: str, tenant_id: UUID, user_id: UUID, session_id: UUID) -> tuple[str, str]:
    """Versioned, purpose-separated 96-bit HMACs over canonical domain ids."""
    material = base64.b64decode(secret, validate=True)

    def digest(purpose: str, identity: UUID) -> str:
        message = f"v1:{purpose}:{tenant_id}:{identity}".encode()
        return hmac.new(material, message, hashlib.sha256).hexdigest()[:24]

    # Change both the message label and wire version together if derivation changes.
    return "lc1u-" + digest("user", user_id), "lc1s-" + digest("session", session_id)
