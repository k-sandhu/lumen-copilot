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

Async callers use the ``*_async`` wrappers, never the sync primitives (#512):
Argon2id is deliberately CPU- and memory-hard, so a verify on the event loop
parks the whole worker for its duration. The wrappers hand the work to a thread
— sound here because the cost is spent inside argon2-cffi's C extension, which
releases the GIL. Sync callers off the request path (the ``seed`` CLI, tests)
keep using the primitives directly.
"""

from __future__ import annotations

import asyncio

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


async def verify_password_async(password_hash: str, password: str) -> bool:
    """:func:`verify_password` off the event loop — the async caller's entry point.

    Identical verdict and identical uniform-failure behaviour; only *where* the
    CPU is spent changes. Callers still wait the full verify, but the loop stays
    free to serve every other request meanwhile (#512).
    """
    return await asyncio.to_thread(verify_password, password_hash, password)


async def dummy_verify_async() -> None:
    """:func:`dummy_verify` off the event loop (#512).

    Still burns a full verify — the cost *is* the point (spec 0004 §2.3), and it
    is what makes the unauthenticated no-such-user path as expensive as a real
    login. Moving it to a thread is therefore not an optimisation to skip: it is
    what stops that path from being a way to park the worker.
    """
    await asyncio.to_thread(dummy_verify)
