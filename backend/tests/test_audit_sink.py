"""Audit sink service — the one injectable ``emit(...)`` (issue #23, spec 0004 §2.4).

The headline is **INV-6**: emitting persists an event carrying *all* required
fields, and an emit missing a required field is **rejected before the write**
(nothing reaches the table). These run against an in-memory async SQLite schema
(offline-safe, like the #44 repository tests) so no Postgres is needed; the
append-only DB grant is exercised by the live migration test, not here.

The sink is deliberately thin: it composes the taxonomy/envelope policy
(``domain/audit``) with the existing append-only ``AuditEventRepository``
(``db/`` — reused, not duplicated). It is the single seam every later feature
calls to satisfy the "auditable" mission filter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.db.repositories import AuditEventRepository
from app.domain.audit import AuditAction, AuditActor, AuditEnvelopeError
from app.domain.entities import AuditOutcome
from app.services.audit import AuditSink

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A fresh in-memory SQLite schema + session per test (offline-safe)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id(session: AsyncSession) -> uuid.UUID:
    """A provisioned tenant to scope the sink to (FK target for audit rows)."""
    from app.db.repositories import TenantRepository

    tenant = await TenantRepository(session).create(name="Acme")
    return tenant.id


def _sink(session: AsyncSession, tenant_id: uuid.UUID) -> AuditSink:
    return AuditSink(AuditEventRepository(session, tenant_id))


# ---------------------------------------------------------------------------
# Happy path — emit persists a complete envelope.
# ---------------------------------------------------------------------------


async def test_emit_persists_event_with_all_required_fields(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """INV-6 (positive): a complete emit persists exactly one event, all fields set."""
    sink = _sink(session, tenant_id)
    actor = AuditActor.user(uuid.uuid4())
    resource = str(uuid.uuid4())

    event = await sink.emit(
        action=AuditAction.DOCUMENT_UPLOADED,
        actor=actor,
        resource_type="document",
        resource_id=resource,
        outcome=AuditOutcome.ALLOWED,
        request_id="req-123",
        source_ip="203.0.113.7",
        metadata={"size_bytes": 42},
    )

    # The returned domain event carries every required field (spec 0004 §2.4).
    assert event.id is not None
    assert event.ts is not None
    assert event.tenant_id == tenant_id
    assert event.actor_id == actor.actor_id
    assert event.action == "document.uploaded"
    assert event.resource_type == "document"
    assert event.resource_id == resource
    assert event.outcome is AuditOutcome.ALLOWED
    assert event.request_id == "req-123"
    assert event.source_ip == "203.0.113.7"
    assert event.metadata == {"size_bytes": 42}

    # And it is the only row, readable back through the same repository.
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert [e.id for e in recent] == [event.id]


async def test_emit_accepts_system_actor(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """A system actor persists with a null actor_id (spec 0004 §2.4)."""
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action=AuditAction.ANSWER_GENERATED,
        actor=AuditActor.system(),
        resource_type="message",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req-sys",
        source_ip="203.0.113.7",
    )
    assert event.actor_id is None


async def test_emit_accepts_anonymous_actor_for_login_failed(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """A failed login has no authenticated actor → anonymous, null actor_id."""
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action=AuditAction.AUTH_LOGIN_FAILED,
        actor=AuditActor.anonymous(),
        resource_type="session",
        outcome=AuditOutcome.DENIED,
        resource_id=str(uuid.uuid4()),
        request_id="req-anon",
        source_ip="203.0.113.7",
    )
    assert event.actor_id is None
    assert event.outcome is AuditOutcome.DENIED


async def test_emit_accepts_taxonomy_action_string(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """A caller may pass the action as a taxonomy string, not only the enum."""
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action="permission.denied",
        actor=AuditActor.user(uuid.uuid4()),
        resource_type="document",
        outcome=AuditOutcome.DENIED,
        resource_id=str(uuid.uuid4()),
        request_id="req-str",
        source_ip="203.0.113.7",
    )
    assert event.action == "permission.denied"


async def test_emit_defaults_metadata_to_empty_dict(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action=AuditAction.AUTH_LOGIN,
        actor=AuditActor.user(uuid.uuid4()),
        resource_type="session",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req-meta",
        source_ip="203.0.113.7",
    )
    assert event.metadata == {}


# ---------------------------------------------------------------------------
# INV-6 (negative) — a missing required field is rejected *before* the write.
# ---------------------------------------------------------------------------


async def test_inv6_missing_resource_type_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """A blank required field raises and persists nothing (fail-closed)."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="",  # required field missing
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="req-1",
            source_ip="203.0.113.7",
        )
    # Nothing was written — the table is empty.
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_unknown_action_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """An action outside the taxonomy is rejected before any write (deny by default)."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action="totally.made.up",
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="req-2",
            source_ip="203.0.113.7",
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_missing_resource_id_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Spec 0004 §2.4: ``resource_id`` is required — a blank value persists nothing."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id="",  # required field missing
            request_id="req-3",
            source_ip="203.0.113.7",
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_missing_request_id_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Spec 0004 §2.4: ``request_id`` is required — a blank value persists nothing."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="   ",  # required field missing
            source_ip="203.0.113.7",
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_missing_source_ip_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Spec 0004 §2.4: ``source_ip`` is required — a blank value persists nothing."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="req-4",
            source_ip="",  # required field missing
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


# ---------------------------------------------------------------------------
# Append-only at the API surface — the sink exposes no mutation path.
# ---------------------------------------------------------------------------


def test_sink_has_no_update_or_delete_method() -> None:
    """Append-only by construction: emit only, no update/delete (spec 0004 §2.4)."""
    names = set(dir(AuditSink))
    assert "emit" in names
    assert "update" not in names
    assert "delete" not in names


# --- the `source_ip` sentinel: why background audit writes used to roll back ---


async def test_a_non_address_source_ip_is_stored_as_null_and_kept_in_metadata(
    session: AsyncSession,
) -> None:
    """A background task's sentinel must not be handed to an ``INET`` column.

    The envelope requires a non-empty ``source_ip`` (spec 0004 §2.4), so a Celery task
    with no client address passes ``"system"``. The column is ``INET`` on Postgres,
    which rejects that — and because the emit rides the CALLER's transaction, the
    rejection rolled back the action being recorded. That is what killed the rolling
    summariser: `session_summaries` rows carried evidence (written in the answer
    transaction) but never a summary or a coverage cursor, and `session.summarized` was
    never written once.

    The sentinel is preserved in metadata rather than dropped: the trail still records
    what the caller stated, and `actor` already says it was the system.
    """
    tenant_id = uuid.uuid4()
    sink = AuditSink(AuditEventRepository(session, tenant_id))

    event = await sink.emit(
        action=AuditAction.SESSION_SUMMARIZED,
        actor=AuditActor.system(),
        resource_type="chat_session",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="celery-summarize",
        source_ip="system",
    )

    # Nothing that is not an address reaches the column.
    assert event.source_ip is None
    # …but the trail keeps what the caller actually stated.
    assert event.source_origin == "system"


async def test_a_real_client_address_is_stored_unchanged(session: AsyncSession) -> None:
    """The request path must be unaffected — a real IP still lands in the column."""
    tenant_id = uuid.uuid4()
    sink = AuditSink(AuditEventRepository(session, tenant_id))

    for address in ("203.0.113.10", "2001:db8::1"):
        event = await sink.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.system(),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="req-1",
            source_ip=address,
        )
        assert event.source_ip == address
        # A real client address means a `client` origin, and nothing in metadata.
        assert event.source_origin == "client"
        assert "source_ip_declared" not in event.metadata


async def test_the_sentinel_does_not_clobber_existing_metadata(session: AsyncSession) -> None:
    """Preserving the sentinel must not drop what the caller was already recording."""
    tenant_id = uuid.uuid4()
    sink = AuditSink(AuditEventRepository(session, tenant_id))

    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="chat_session",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="celery-1",
        source_ip="system",
        metadata={"covered_messages": 12},
    )

    assert event.metadata["covered_messages"] == 12
    assert event.source_origin == "system"


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        # Plain addresses, stored canonically.
        ("203.0.113.10", "203.0.113.10"),
        ("2001:db8::1", "2001:db8::1"),
        ("::ffff:1.2.3.4", "::ffff:1.2.3.4"),
        ("  192.168.1.5  ", "192.168.1.5"),
        # `INET` accepts CIDR too.
        ("10.0.0.0/8", "10.0.0.0/8"),
        # ZONE-SCOPED: Python parses it, Postgres does NOT — storing the raw text would
        # reproduce the very rollback this function exists to prevent, for any
        # link-local peer. The zone is dropped and the address kept.
        ("fe80::1%eth0", "fe80::1"),
        # BRACKETED / port-bearing: not an address to Python, so a naive parse would
        # silently NULL a REAL client address and lose audit fidelity.
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
    ],
)
async def test_real_addresses_survive_in_every_form_a_client_can_present(
    session: AsyncSession, given: str, stored: str
) -> None:
    """The regression that would matter most: silently NULLing genuine client IPs."""
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip=given,
    )
    assert event.source_ip == stored
    assert event.source_origin == "client"


async def test_a_caller_metadata_key_named_source_ip_is_not_clobbered(
    session: AsyncSession,
) -> None:
    """The collision the previous test only claimed to cover.

    An audit event may legitimately carry an upstream `source_ip` of its own; the
    preserved envelope value is a DIFFERENT fact, so it gets its own namespaced key.
    """
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip="system",
        metadata={"source_ip": "198.51.100.7"},
    )
    assert event.metadata["source_ip"] == "198.51.100.7"  # the caller's, untouched
    assert event.source_origin == "system"  # ours, alongside it


async def test_the_request_path_sentinel_is_handled_too(session: AsyncSession) -> None:
    """`request.client` is None for an AF_UNIX peer, so the app sends "unknown".

    Without this, a socket-mode proxy deployment would NULL `source_ip` on every event
    for genuine user traffic — silently, since the coercion never raises.
    """
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip="unknown",
    )
    # `unknown` is deliberately NOT `system`: a person did this and we could not see
    # from where, which is a different operational fact from "the platform did this".
    assert event.source_ip is None
    assert event.source_origin == "unknown"


async def test_an_unrecognised_non_address_is_recorded_as_unknown_not_system(
    session: AsyncSession,
) -> None:
    """A misconfigured proxy must not masquerade as background work.

    Coercing anything unparseable to `system` would hide a real operational problem
    behind a legitimate-looking origin — and would let a client action be filed as a
    platform action, which is exactly the confusion `source_origin` exists to remove.
    """
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip="proxy-did-something-odd",
    )
    assert event.source_origin == "unknown"
    assert event.source_ip is None


async def test_the_database_refuses_a_pair_that_contradicts_itself(
    session: AsyncSession,
) -> None:
    """The origin/address agreement is a constraint, not a convention.

    `AuditEventRepository.record` cannot currently produce a contradictory pair, but
    it is not the only thing that will ever write this table — a later writer, a
    backfill, or a hand-run repair could. Pinning the rule in the schema means such a
    write fails loudly at the boundary instead of quietly filing a person's action as
    the platform's (or the reverse).
    """
    tenant_id = uuid.uuid4()
    for origin, ip in (("client", None), ("system", "203.0.113.10")):
        session.add(
            models.AuditEvent(
                tenant_id=tenant_id,
                actor_id=None,
                action=AuditAction.RETRIEVAL_QUERY.value,
                resource_type="document",
                resource_id=str(uuid.uuid4()),
                outcome=AuditOutcome.ALLOWED.value,
                request_id="req",
                source_origin=origin,
                source_ip=ip,
                event_metadata={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
