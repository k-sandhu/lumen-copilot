"""Envelope-encryption unit tests — the secrets-vault cipher (issue #209 AC-2).

Pure, offline crypto over ``bytes`` (``app.core.crypto``): no DB, no network. The
security-load-bearing properties the vault rests on:

* ciphertext at rest is **not** the plaintext (AC-2);
* it round-trips only with the configured key;
* a **wrong / rotated** key (or tampered ciphertext/nonce) **fails closed** —
  raises rather than returning any bytes (AC-2, the negative);
* each encryption uses a fresh nonce, so encrypting the same plaintext twice does
  not produce identical ciphertext (no deterministic leakage / nonce reuse);
* a malformed master key is rejected at construction (fail fast).
"""

from __future__ import annotations

import base64

import pytest

from app.core.crypto import (
    CURRENT_KEY_VERSION,
    EncryptedSecret,
    SecretDecryptionError,
    SecretsCipher,
    SecretsCryptoError,
    generate_master_key,
    load_master_key,
)


def test_ciphertext_is_not_plaintext_and_round_trips() -> None:
    """AC-2: the stored ciphertext differs from the plaintext and decrypts back."""
    cipher = SecretsCipher(generate_master_key())
    plaintext = "sk-super-secret-mcp-token-abcd1234"

    env = cipher.encrypt(plaintext)

    # At rest: neither the raw bytes nor any encoding of them equals the plaintext.
    assert env.ciphertext != plaintext.encode("utf-8")
    assert plaintext.encode("utf-8") not in env.ciphertext
    assert env.key_version == CURRENT_KEY_VERSION
    # Round-trips with the same key.
    assert cipher.decrypt(env) == plaintext


def test_wrong_key_fails_closed() -> None:
    """AC-2 (negative): a different key cannot decrypt — it raises, returns nothing.

    The rotated/wrong-key case: a ciphertext produced under key A decrypted with
    key B must fail closed (``SecretDecryptionError``), never yield plaintext or
    garbage.
    """
    cipher_a = SecretsCipher(generate_master_key())
    cipher_b = SecretsCipher(generate_master_key())
    env = cipher_a.encrypt("top-secret-value")

    with pytest.raises(SecretDecryptionError):
        cipher_b.decrypt(env)


def test_tampered_ciphertext_fails_closed() -> None:
    """A single flipped ciphertext byte fails the GCM auth tag (fail-closed)."""
    cipher = SecretsCipher(generate_master_key())
    env = cipher.encrypt("integrity-matters")
    tampered = bytearray(env.ciphertext)
    tampered[0] ^= 0x01  # flip one bit
    bad = EncryptedSecret(
        ciphertext=bytes(tampered), nonce=env.nonce, key_version=env.key_version
    )

    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(bad)


def test_tampered_nonce_fails_closed() -> None:
    """A changed nonce also fails authentication (the nonce is bound to the tag)."""
    cipher = SecretsCipher(generate_master_key())
    env = cipher.encrypt("integrity-matters")
    tampered_nonce = bytearray(env.nonce)
    tampered_nonce[0] ^= 0x01
    bad = EncryptedSecret(
        ciphertext=env.ciphertext, nonce=bytes(tampered_nonce), key_version=env.key_version
    )

    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(bad)


def test_same_plaintext_encrypts_to_different_ciphertext() -> None:
    """Fresh per-encryption nonce ⇒ no deterministic ciphertext (no nonce reuse)."""
    cipher = SecretsCipher(generate_master_key())
    a = cipher.encrypt("same-value")
    b = cipher.encrypt("same-value")

    assert a.nonce != b.nonce
    assert a.ciphertext != b.ciphertext
    # Both still decrypt to the same plaintext.
    assert cipher.decrypt(a) == cipher.decrypt(b) == "same-value"


def test_unicode_plaintext_round_trips() -> None:
    """Non-ASCII credentials survive the UTF-8 encode/decode round-trip."""
    cipher = SecretsCipher(generate_master_key())
    plaintext = "clé-secrète-日本語-🔑"
    assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext


def test_generate_master_key_is_valid_material() -> None:
    """The generated key is base64 of exactly 32 bytes (AES-256) and loads."""
    key = generate_master_key()
    assert len(base64.b64decode(key)) == 32
    assert len(load_master_key(key)) == 32


@pytest.mark.parametrize(
    "bad_key",
    [
        "not base64!!!",  # not valid base64
        base64.b64encode(b"too-short").decode(),  # valid base64, < 32 bytes
        base64.b64encode(b"x" * 33).decode(),  # valid base64, > 32 bytes
        "",  # empty
    ],
)
def test_malformed_master_key_rejected(bad_key: str) -> None:
    """A key that is not base64-of-32-bytes is rejected (fail fast at construction)."""
    with pytest.raises(SecretsCryptoError):
        SecretsCipher(bad_key)
