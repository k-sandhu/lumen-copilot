"""Password hashing — Argon2id, per-user salt (spec 0004 §2.3).

The single owner of password hashing/verification. Argon2id (the hybrid,
side-channel- and GPU-resistant variant) is the decided algorithm; ``argon2-cffi``
generates a fresh random salt per hash and encodes the parameters in the stored
PHC string, so verification needs nothing but the hash and the candidate.

Verification is **constant-time-ish and uniform on failure**: a wrong password
and an unknown user must be indistinguishable to the caller (no account-existence
disclosure, spec 0004 §2.3 / contract login 401). The service layer pairs this
with a dummy-verify on the no-user path; this module just never reveals *why* a
verify failed beyond a bool.
"""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

# One process-wide hasher. Defaults follow argon2-cffi's RFC-9106-aligned
# recommendations; pinned here (not config) because changing cost parameters is
# a deliberate security decision, and old hashes still verify (params are in the
# PHC string). ``type=ID`` selects Argon2id explicitly.
_hasher = PasswordHasher(type=Type.ID)

# A precomputed Argon2id hash of a throwaway value. Verifying against it on the
# "user not found" path burns the same CPU as a real verify, so login timing
# does not leak whether an email exists (spec 0004 §2.3).
_DUMMY_HASH = _hasher.hash("dummy-password-for-uniform-timing")


def hash_password(password: str) -> str:
    """Return an Argon2id PHC-string hash (random per-call salt embedded)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return True iff ``password`` matches ``password_hash``.

    Never raises on a mismatch or a malformed stored hash — returns ``False`` —
    so callers branch on a bool and cannot distinguish failure modes.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify() -> None:
    """Burn a verify's worth of CPU on the no-such-user path (timing uniformity).

    Called by the login service when no user matches the supplied email, so the
    response time is independent of account existence (spec 0004 §2.3).
    """
    try:
        _hasher.verify(_DUMMY_HASH, "wrong")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass
