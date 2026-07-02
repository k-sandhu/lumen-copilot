"""Envelope encryption for the secrets vault — the single cipher owner (#209).

The **one** module in the backend that imports a cipher (ADR-0004 chokepoint:
"one owning module means one place to get envelope-encryption … right"). Nothing
else may import ``cryptography``; ``services/secrets_service.py`` is the sole
caller of this module, and it is the only path that turns a plaintext credential
into ciphertext at rest and back (spec 0004's "deny by default" applied to
credential material — a plaintext never lives in a column, a log, or a wire
response).

**Cipher: AES-256-GCM** (an *authenticated* cipher — the issue's requirement).
GCM binds a 128-bit authentication tag to every ciphertext, so tampering or the
wrong key is detected on decrypt and raises rather than returning garbage
(fail-closed). Each :meth:`SecretsCipher.encrypt` draws a **fresh 96-bit nonce**
(the GCM standard size) from ``os.urandom``; the nonce is stored beside the
ciphertext (it is not secret, only unique-per-key) so decrypt can reconstruct the
box. Never reuse a (key, nonce) pair — the per-call random nonce guarantees that.

**Envelope / key management.** The master key comes from ``core/config.py``
(``SECRETS_ENCRYPTION_KEY``, base64) — the config chokepoint, never an
``os.environ`` read here. ``key_version`` is carried alongside each ciphertext so
a later rotation can add a new key version and decrypt old rows with the old one;
the MVP has exactly one version (:data:`CURRENT_KEY_VERSION`). External-KMS
integration is a deliberate future seam (issue #209 scope fence "OUT"): swapping
this class's key source for a KMS ``Decrypt`` call is a localized change, since
this is the only module that holds the key material.

This module is pure crypto over ``bytes``: no ORM, no framework, no I/O beyond the
OS RNG. The typed :class:`EncryptedSecret` it returns is what the repository
persists column-for-column.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 → a 32-byte key. GCM's standard nonce is 96 bits (12 bytes); the auth
# tag GCM appends is 128 bits (16 bytes), included in the returned ciphertext.
_KEY_BYTES = 32
_NONCE_BYTES = 12

# The MVP key version. ``key_version`` is stored per row so a future rotation adds
# a new version (and keeps the old key to decrypt old rows) without a schema change.
CURRENT_KEY_VERSION = 1


class SecretsCryptoError(Exception):
    """Base for secrets-crypto failures (bad key material, or a failed decrypt).

    A domain-ish error kept in ``core`` because the cipher lives here; the
    calling service decides how (if at all) to surface it. Crucially, a *failed
    decrypt* (wrong key, tampered ciphertext/nonce) raises this rather than
    returning plaintext — the vault fails **closed**.
    """


class SecretDecryptionError(SecretsCryptoError):
    """Decryption failed — wrong key/version, or tampered ciphertext or nonce.

    AES-GCM's authentication tag did not verify. Raised instead of returning any
    bytes, so a caller can never receive garbage-as-plaintext (fail-closed). The
    message is deliberately generic (no key/nonce detail) so it is safe to log.
    """


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """The ciphertext envelope for one secret — what the repository stores.

    ``ciphertext`` is the GCM output (encrypted bytes ‖ 128-bit auth tag);
    ``nonce`` is the 96-bit per-encryption nonce (stored, not secret);
    ``key_version`` records which master-key version produced it (rotation-ready).
    None of these fields is or reveals the plaintext.
    """

    ciphertext: bytes
    nonce: bytes
    key_version: int


def load_master_key(encoded: str) -> bytes:
    """Decode + validate the base64 ``SECRETS_ENCRYPTION_KEY`` into raw key bytes.

    Called once by :class:`SecretsCipher`. Enforces that the configured key is
    real 256-bit key material: a value that is not valid base64, or does not
    decode to exactly 32 bytes, raises :class:`SecretsCryptoError` — so a
    misconfigured key fails at cipher construction (startup, via the settings
    fail-fast), not deep in a store/retrieve call.

    Raises:
        SecretsCryptoError: the value is not valid base64 or is not 32 bytes.
    """
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretsCryptoError(
            "SECRETS_ENCRYPTION_KEY is not valid base64 (expected base64 of 32 bytes)"
        ) from exc
    if len(raw) != _KEY_BYTES:
        raise SecretsCryptoError(
            f"SECRETS_ENCRYPTION_KEY must decode to {_KEY_BYTES} bytes "
            f"(AES-256); got {len(raw)}"
        )
    return raw


def generate_master_key() -> str:
    """Return a fresh base64-encoded 256-bit master key (ops/key-provisioning helper).

    Not used at runtime — a convenience so an operator (or a test) can mint a
    valid ``SECRETS_ENCRYPTION_KEY`` without hand-rolling ``openssl``. The bytes
    come from ``os.urandom`` (a CSPRNG).
    """
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode("ascii")


class SecretsCipher:
    """AES-256-GCM envelope cipher over ``bytes`` — the only cipher in the backend.

    Constructed from the base64 master key (``SECRETS_ENCRYPTION_KEY``). Holds the
    raw key in memory only; the sole importer is
    :class:`~app.services.secrets_service.SecretsService`. Stateless per call: each
    :meth:`encrypt` draws its own random nonce, so the same instance safely
    encrypts many secrets.
    """

    def __init__(self, encoded_master_key: str) -> None:
        self._key = load_master_key(encoded_master_key)
        self._aesgcm = AESGCM(self._key)

    def encrypt(self, plaintext: str) -> EncryptedSecret:
        """Encrypt a UTF-8 plaintext into a stored envelope (fresh random nonce).

        Returns an :class:`EncryptedSecret` carrying the ciphertext (with GCM's
        auth tag appended), the 96-bit nonce, and :data:`CURRENT_KEY_VERSION`. The
        ciphertext is never equal to the plaintext bytes (asserted by the AC-2
        test). The plaintext is not retained.
        """
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return EncryptedSecret(
            ciphertext=ciphertext, nonce=nonce, key_version=CURRENT_KEY_VERSION
        )

    def decrypt(self, envelope: EncryptedSecret) -> str:
        """Decrypt a stored envelope back to its UTF-8 plaintext, or fail closed.

        Verifies GCM's authentication tag with the configured key; on any mismatch
        — wrong key/version, or a tampered ciphertext or nonce — raises
        :class:`SecretDecryptionError` **instead of** returning bytes. So a vault
        that cannot prove authenticity yields nothing (the AC-2 wrong-key test).

        Raises:
            SecretDecryptionError: the ciphertext did not authenticate under the
                configured key (fail-closed).
        """
        try:
            plaintext = self._aesgcm.decrypt(envelope.nonce, envelope.ciphertext, None)
        except InvalidTag as exc:
            # Generic message on purpose — never echo key/nonce/ciphertext detail.
            raise SecretDecryptionError(
                "secret could not be decrypted with the configured key"
            ) from exc
        return plaintext.decode("utf-8")


__all__ = [
    "CURRENT_KEY_VERSION",
    "EncryptedSecret",
    "SecretDecryptionError",
    "SecretsCipher",
    "SecretsCryptoError",
    "generate_master_key",
    "load_master_key",
]
