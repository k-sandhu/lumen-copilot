"""Auth primitive unit tests — hashing + JWT (issue #19, spec 0004 §2.3).

Pure and offline: no DB, no app. They pin the security-load-bearing behavior of
``app.auth.hashing`` (Argon2id round-trip, uniform failure) and
``app.auth.tokens`` (JWT mint/verify claims, and the INV-4 negative cases where
a missing/expired/tampered/wrong-issuer token must fail closed → ``InvalidTokenError``).
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth import (
    InvalidTokenError,
    Principal,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    hashing,
    mint_access_token,
    verify_access_token,
    verify_password,
)
from app.auth.hashing import dummy_verify
from app.core.config import Settings, get_settings
from app.domain.entities import Role


@pytest.fixture
def settings() -> Settings:
    """Test settings (env seeded by conftest); short access TTL for expiry tests."""
    get_settings.cache_clear()
    return get_settings()


def _principal() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        roles=(Role.MEMBER, Role.ADMIN),
    )


# --- Argon2id password hashing ---------------------------------------------


def test_hash_password_round_trips() -> None:
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"  # never store the plaintext
    assert h.startswith("$argon2id$")  # Argon2id PHC string (spec 0004 §2.3)
    assert verify_password(h, "correct horse battery staple") is True


def test_hash_password_salts_per_call() -> None:
    # Same password, two hashes → different (random per-call salt).
    assert hash_password("pw") != hash_password("pw")


def test_verify_password_rejects_wrong_and_malformed() -> None:
    h = hash_password("pw")
    assert verify_password(h, "nope") is False
    # A malformed stored hash returns False, never raises (uniform failure).
    assert verify_password("not-a-real-hash", "pw") is False


def test_dummy_verify_does_not_raise() -> None:
    # The no-such-user timing path must complete silently.
    dummy_verify()


# --- Off-loop verification (#512) ------------------------------------------
#
# Argon2id is deliberately CPU- and memory-hard, and the unknown-user path burns
# the *same* cost by design (timing uniformity, above). Run inline on the event
# loop, that cost parks the whole worker for the duration of every login attempt
# — including attempts for accounts that do not exist. The async wrappers hand
# the work to a thread (Argon2 releases the GIL in its C extension), so these
# tests pin **where** the work runs, not merely that it returns the right answer.


async def test_verify_password_async_matches_the_sync_result() -> None:
    # Same answers as verify_password: the thread hop changes latency ownership,
    # never the verdict.
    h = hash_password("correct horse battery staple")
    assert await hashing.verify_password_async(h, "correct horse battery staple") is True
    assert await hashing.verify_password_async(h, "nope") is False
    # A malformed stored hash still returns False and never raises.
    assert await hashing.verify_password_async("not-a-real-hash", "pw") is False


async def test_verify_password_async_runs_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Argon2 work lands on a worker thread, never the loop's own."""
    ran_on: list[int] = []

    def _record(password_hash: str, password: str) -> bool:  # noqa: ARG001
        ran_on.append(threading.get_ident())
        return True

    monkeypatch.setattr(hashing, "verify_password", _record)
    assert await hashing.verify_password_async("hash", "pw") is True
    assert ran_on == [ran_on[0]] and ran_on[0] != threading.get_ident()


async def test_dummy_verify_async_still_pays_the_cost_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unknown-user path keeps burning a verify — just not on the loop.

    Delegation is the load-bearing assertion: skipping the work would make the
    no-such-user path cheap and reintroduce the account-existence timing oracle
    that :func:`dummy_verify` exists to close (spec 0004 §2.3).
    """
    ran_on: list[int] = []

    def _record() -> None:
        ran_on.append(threading.get_ident())

    monkeypatch.setattr(hashing, "dummy_verify", _record)
    await hashing.dummy_verify_async()
    assert ran_on == [ran_on[0]] and ran_on[0] != threading.get_ident()


async def test_verification_concurrency_is_bounded_by_a_dedicated_pool() -> None:
    """Concurrent verifies are capped, and never run on the shared default executor.

    Argon2id is memory-hard, so verifications in flight are a memory budget: on
    the shared ``asyncio.to_thread`` executor an unauthenticated login burst
    could both multiply that budget without limit and starve every other
    ``to_thread`` caller. This pins the ceiling and the isolation (review round 1,
    finding 2).
    """
    cap = hashing._MAX_CONCURRENT_VERIFICATIONS
    assert 2 <= cap <= 8  # a memory ceiling, not an unbounded pool

    in_flight = 0
    peak = 0
    threads: set[str] = set()
    lock = threading.Lock()
    release = threading.Event()

    def _blocking_verify(password_hash: str, password: str) -> bool:  # noqa: ARG001
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
            threads.add(threading.current_thread().name)
        # Hold every worker until the pool is provably saturated, so `peak`
        # reflects the pool's real ceiling rather than how fast verifies retire.
        release.wait(timeout=5.0)
        with lock:
            in_flight -= 1
        return True

    original = hashing.verify_password
    hashing.verify_password = _blocking_verify  # type: ignore[assignment]
    try:
        # Ask for more than the cap; the surplus must queue, not run.
        tasks = [
            asyncio.create_task(hashing.verify_password_async("h", "pw")) for _ in range(cap + 4)
        ]
        # Let the pool fill, then let everyone go.
        await asyncio.sleep(0.2)
        observed_peak = peak
        release.set()
        assert all(await asyncio.gather(*tasks))
    finally:
        release.set()
        hashing.verify_password = original  # type: ignore[assignment]

    assert observed_peak == cap
    # Its own named threads — not the shared default executor's.
    assert threads and all(name.startswith("argon2-verify") for name in threads)


async def test_both_login_paths_share_one_verification_queue() -> None:
    """The known-user and unknown-user paths must saturate together.

    Routing them to separate executors would give them different queueing
    behaviour under load and reintroduce the account-existence timing signal
    that ``dummy_verify`` exists to close (spec 0004 §2.3).
    """
    seen: set[str] = set()
    release = threading.Event()

    def _record(*_args: object) -> bool:
        seen.add(threading.current_thread().name)
        release.wait(timeout=5.0)
        return True

    original_verify = hashing.verify_password
    original_dummy = hashing.dummy_verify
    hashing.verify_password = _record  # type: ignore[assignment]
    hashing.dummy_verify = _record  # type: ignore[assignment]
    try:
        tasks = [
            asyncio.create_task(hashing.verify_password_async("h", "pw")),
            asyncio.create_task(hashing.dummy_verify_async()),
        ]
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.gather(*tasks)
    finally:
        release.set()
        hashing.verify_password = original_verify  # type: ignore[assignment]
        hashing.dummy_verify = original_dummy  # type: ignore[assignment]

    # Both landed on the one dedicated pool.
    assert seen and all(name.startswith("argon2-verify") for name in seen)


# --- Refresh tokens (opaque; hashed at rest) -------------------------------


def test_refresh_token_is_opaque_and_unique() -> None:
    a = generate_refresh_token()
    b = generate_refresh_token()
    assert a != b
    assert len(a) >= 32


def test_refresh_token_hash_is_deterministic_and_hides_token() -> None:
    raw = generate_refresh_token()
    assert hash_refresh_token(raw) == hash_refresh_token(raw)  # deterministic lookup
    assert hash_refresh_token(raw) != raw  # the raw token is never the stored value


# --- Access JWT mint / verify ----------------------------------------------


def test_mint_then_verify_round_trips_claims(settings: Settings) -> None:
    principal = _principal()
    minted = mint_access_token(principal, settings)
    assert minted.expires_in == settings.access_token_ttl_seconds

    resolved = verify_access_token(minted.token, settings)
    assert resolved.user_id == principal.user_id
    assert resolved.tenant_id == principal.tenant_id
    assert resolved.roles == principal.roles


def test_verify_rejects_garbage_token(settings: Settings) -> None:
    with pytest.raises(InvalidTokenError):
        verify_access_token("not.a.jwt", settings)


def test_verify_rejects_wrong_signature(settings: Settings) -> None:
    principal = _principal()
    forged = jwt.encode(
        {
            "sub": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "roles": ["member"],
            "iss": settings.jwt_issuer,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        "the-wrong-secret",
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        verify_access_token(forged, settings)


def test_verify_rejects_wrong_issuer(settings: Settings) -> None:
    principal = _principal()
    token = jwt.encode(
        {
            "sub": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "roles": ["member"],
            "iss": "some-other-issuer",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        verify_access_token(token, settings)


def test_verify_rejects_expired_token(settings: Settings) -> None:
    principal = _principal()
    token = jwt.encode(
        {
            "sub": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "roles": ["member"],
            "iss": settings.jwt_issuer,
            "iat": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
            "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        verify_access_token(token, settings)


def test_verify_rejects_missing_required_claims(settings: Settings) -> None:
    # No tenant_id claim → malformed → 401 (a token must be tenant-bound).
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iss": settings.jwt_issuer,
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        verify_access_token(token, settings)


def test_verify_rejects_non_uuid_subject(settings: Settings) -> None:
    token = jwt.encode(
        {
            "sub": "not-a-uuid",
            "tenant_id": str(uuid.uuid4()),
            "roles": ["member"],
            "iss": settings.jwt_issuer,
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        verify_access_token(token, settings)


def test_access_token_actually_expires_at_runtime(settings: Settings) -> None:
    """A 1-second token is rejected once the second elapses (real-clock INV-4)."""
    # Build a settings whose access TTL is 1s by overriding the env-derived value
    # via a fresh Settings with the cap-respecting value.
    short = settings.model_copy(update={"access_token_ttl_seconds": 1})
    minted = mint_access_token(_principal(), short)
    # Immediately valid...
    verify_access_token(minted.token, short)
    # ...and invalid after it expires (allow PyJWT's default 0s leeway + buffer).
    time.sleep(2)
    with pytest.raises(InvalidTokenError):
        verify_access_token(minted.token, short)
