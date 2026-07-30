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
parks the whole worker for its duration. The wrappers hand the work to a
**dedicated, bounded** pool — sound here because the cost is spent inside
argon2-cffi's C extension, which releases the GIL. Sync callers off the request
path (the ``seed`` CLI, tests) keep using the primitives directly.

The pool is deliberately *not* ``asyncio.to_thread``'s shared default executor.
Argon2id's cost is memory-hard by design, so the number of verifications in
flight is a **memory budget**, not merely a CPU one; an unauthenticated login
burst on the shared executor could both multiply that budget without limit and
starve every other ``to_thread`` caller in the process. Sizing the pool caps
concurrent verifications explicitly — excess logins queue rather than pile up
(review round 1, finding 2).
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

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


# Concurrent verifications allowed in flight. Each costs the pinned Argon2id
# memory parameter (argon2-cffi's RFC-9106-aligned default, 64 MiB), so this is
# a memory ceiling as much as a parallelism one: 2-8 workers bounds the pool at
# roughly 128-512 MiB under saturation. Excess logins queue on the pool, which
# is the desired shape — a queued login is slow, an unbounded one is an outage.
_MAX_CONCURRENT_VERIFICATIONS = max(2, min(8, os.cpu_count() or 2))

# Dedicated so a login burst cannot occupy the shared default executor that every
# other ``asyncio.to_thread`` caller in the process depends on, nor be starved by
# them. Threads are created lazily and joined at interpreter exit.
_verify_pool = ThreadPoolExecutor(
    max_workers=_MAX_CONCURRENT_VERIFICATIONS,
    thread_name_prefix="argon2-verify",
)


async def verify_password_async(password_hash: str, password: str) -> bool:
    """:func:`verify_password` off the event loop — the async caller's entry point.

    Identical verdict and identical uniform-failure behaviour; only *where* the
    CPU is spent changes. Callers still wait the full verify, but the loop stays
    free to serve every other request meanwhile (#512).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_verify_pool, verify_password, password_hash, password)


async def dummy_verify_async() -> None:
    """:func:`dummy_verify` off the event loop (#512).

    Still burns a full verify — the cost *is* the point (spec 0004 §2.3), and it
    is what makes the unauthenticated no-such-user path as expensive as a real
    login. Moving it to a thread is therefore not an optimisation to skip: it is
    what stops that path from being a way to park the worker.

    Runs on the **same** bounded pool as :func:`verify_password_async`, so the
    two login paths share one queue and one memory budget. Routing them to
    different executors would give them different saturation behaviour and
    reintroduce, under load, exactly the account-existence timing signal that
    burning a real verify here exists to close (spec 0004 §2.3).
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_verify_pool, dummy_verify)
