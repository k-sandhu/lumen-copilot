"""Secrets vault — the write-only service + its negative tests (issue #209).

The point of the vault (issue #209): an encrypted, per-tenant store for
third-party credentials that is **write-only + list-metadata + delete** through
any surface, **audited**, and whose plaintext is reachable only in-process. These
exercise the service (``app.services.secrets_service``) end-to-end on offline
in-memory SQLite — the ``secrets`` table is plain relational SQL + ``bytea``
(``BLOB`` on SQLite), so the whole store/list/delete/internal-get path and the
AES-256-GCM envelope run without Postgres.

Headlines mapped to the acceptance criteria:

* **AC-1** — store→list shows metadata + hint but never the value; store→internal
  ``get_secret_plaintext`` round-trips the plaintext.
* **AC-2** — a wrong/rotated key fails closed at the service boundary (no
  plaintext), on top of the pure-crypto proof in ``test_crypto.py``.
* **AC-4** — a cross-tenant / non-owned secret id → 404 (INV-1/INV-2), never 403;
  no leak that it exists.
* **AC-5** — create / access / delete each emit an audit event (INV-6);
  ``secret.accessed`` names the reader, not the value.

(AC-3 — "no router returns a secret value" — is an architecture/contract test in
``test_secrets_no_router.py``.)
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.crypto import SecretDecryptionError, SecretsCipher, generate_master_key
from app.core.errors import NotFoundError, ValidationError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    SecretRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import Role, SecretKind
from app.services.audit import AuditSink
from app.services.secrets_service import SecretsService

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


# ---------------------------------------------------------------------------
# Offline fixtures: in-memory SQLite schema + two tenants (mirrors test_grants).
# ---------------------------------------------------------------------------


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
async def two_tenants(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenants = TenantRepository(session)
    a = await tenants.create(name="Tenant A")
    b = await tenants.create(name="Tenant B")
    return a.id, b.id


async def _make_user(session: AsyncSession, tenant_id: uuid.UUID, email: str) -> uuid.UUID:
    user = await UserRepository(session, tenant_id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    return user.id


# A single cipher shared across a test unless a test explicitly needs a second
# (rotated) key — most tests only care that store/get round-trips.
_CIPHER = SecretsCipher(generate_master_key())


def _secrets_service(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    roles: tuple[Role, ...] = (Role.MEMBER,),
    cipher: SecretsCipher = _CIPHER,
) -> SecretsService:
    audit = AuditSink(AuditEventRepository(session, tenant_id))
    return SecretsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        cipher=cipher,
        audit=audit,
        request_id="req-test",
        source_ip="203.0.113.1",
    )


async def _audit_actions(session: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    events = await AuditEventRepository(session, tenant_id).list_recent(limit=20)
    return {e.action for e in events}


# ---------------------------------------------------------------------------
# AC-1: store → list shows metadata + hint, never the value; internal-get
# round-trips the plaintext.
# ---------------------------------------------------------------------------


async def test_store_returns_ref_without_plaintext(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-1: ``store_secret`` returns id + metadata + masked hint, never the value."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    plaintext = "sk-mcp-abcdEFGH1234"
    ref = await svc.store_secret(name="github-mcp", kind=SecretKind.MCP_AUTH, plaintext=plaintext)

    assert ref.name == "github-mcp"
    assert ref.kind is SecretKind.MCP_AUTH
    assert ref.owner_id == user_a
    # The hint is a masked tail — reveals only the last 4 chars, never the value.
    assert ref.hint == "****1234"
    assert plaintext not in ref.hint
    # The SecretRef dataclass has no field that could carry the value/ciphertext.
    assert not hasattr(ref, "ciphertext")
    assert not hasattr(ref, "plaintext")
    assert not hasattr(ref, "value")


async def test_list_shows_metadata_and_hint_never_value(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-1: ``list_secrets`` yields metadata + hint only (no value, no ciphertext)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    await svc.store_secret(name="one", kind=SecretKind.MCP_AUTH, plaintext="value-one-XXXX")
    await svc.store_secret(name="two", kind=SecretKind.SEARCH_API, plaintext="value-two-YYYY")

    refs = await svc.list_secrets()
    assert {r.name for r in refs} == {"one", "two"}
    by_name = {r.name: r for r in refs}
    assert by_name["one"].hint == "****XXXX"
    assert by_name["two"].kind is SecretKind.SEARCH_API
    # No ref exposes a plaintext/ciphertext attribute.
    for r in refs:
        assert not hasattr(r, "ciphertext")
        assert not hasattr(r, "value")


async def test_internal_get_round_trips_plaintext(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-1: the owner's internal ``get_secret_plaintext`` returns the exact value."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    plaintext = "hosted-search-key-9f8e7d"
    ref = await svc.store_secret(
        name="search", kind=SecretKind.SEARCH_API, plaintext=plaintext
    )
    got = await svc.get_secret_plaintext(ref.id)
    assert got == plaintext


async def test_ciphertext_at_rest_is_not_plaintext(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-2: the persisted row holds ciphertext + nonce, never the plaintext."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    plaintext = "plain-value-to-check-at-rest"
    ref = await svc.store_secret(name="k", kind=SecretKind.OTHER, plaintext=plaintext)

    # Read the raw row through the repository (the ciphertext the DB actually holds).
    stored = await SecretRepository(session, tenant_a).get(ref.id)
    assert stored is not None
    assert stored.ciphertext != plaintext.encode("utf-8")
    assert plaintext.encode("utf-8") not in stored.ciphertext
    assert len(stored.nonce) == 12  # GCM nonce persisted alongside


async def test_store_same_name_rotates_value_in_place(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Re-storing a name rotates the value (stable handle) — get returns the new one."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    ref1 = await svc.store_secret(name="rot", kind=SecretKind.MCP_AUTH, plaintext="old-AAAA1111")
    ref2 = await svc.store_secret(name="rot", kind=SecretKind.MCP_AUTH, plaintext="new-BBBB2222")

    assert ref1.id == ref2.id  # same handle
    assert ref2.hint == "****2222"
    assert await svc.get_secret_plaintext(ref2.id) == "new-BBBB2222"
    # Exactly one row for that name (no duplicate).
    assert len(await svc.list_secrets()) == 1


# ---------------------------------------------------------------------------
# AC-2 (service level): a wrong/rotated key fails closed — no plaintext.
# ---------------------------------------------------------------------------


async def test_wrong_key_fails_closed_at_service(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-2: a secret stored under key A cannot be read under key B (fails closed).

    Simulates a mis-rotated / wrong master key at the boundary an adapter uses: the
    stored envelope decrypted with a different configured key raises rather than
    returning any plaintext.
    """
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")

    key_a = SecretsCipher(generate_master_key())
    key_b = SecretsCipher(generate_master_key())

    ref = await _secrets_service(
        session, tenant_id=tenant_a, owner_id=user_a, cipher=key_a
    ).store_secret(name="k", kind=SecretKind.MCP_AUTH, plaintext="secret-under-key-a")

    svc_wrong_key = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a, cipher=key_b)
    with pytest.raises(SecretDecryptionError):
        await svc_wrong_key.get_secret_plaintext(ref.id)


# ---------------------------------------------------------------------------
# AC-4: cross-tenant / non-owned → 404 (INV-1/INV-2), never a leak.
# ---------------------------------------------------------------------------


async def test_cross_tenant_get_is_404(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-4/INV-1: a tenant-B caller reading a tenant-A secret id → 404 (not 403)."""
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_b, "b@y.test")

    ref = await _secrets_service(
        session, tenant_id=tenant_a, owner_id=user_a
    ).store_secret(name="a-secret", kind=SecretKind.MCP_AUTH, plaintext="a-only-value")

    svc_b = _secrets_service(session, tenant_id=tenant_b, owner_id=user_b)
    with pytest.raises(NotFoundError):
        await svc_b.get_secret_plaintext(ref.id)
    with pytest.raises(NotFoundError):
        await svc_b.delete_secret(ref.id)


async def test_non_owner_same_tenant_get_is_404(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-4/INV-2: a non-owner (non-admin) in the same tenant → 404 (existence hidden)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")

    ref = await _secrets_service(
        session, tenant_id=tenant_a, owner_id=user_a
    ).store_secret(name="a-secret", kind=SecretKind.MCP_AUTH, plaintext="a-value-1234")

    svc_b = _secrets_service(session, tenant_id=tenant_a, owner_id=user_b)
    with pytest.raises(NotFoundError):
        await svc_b.get_secret_plaintext(ref.id)
    with pytest.raises(NotFoundError):
        await svc_b.delete_secret(ref.id)
    # B does not even see it in their own list.
    assert await svc_b.list_secrets() == []


async def test_tenant_admin_may_access_and_delete_any_secret(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A tenant admin may read/use/delete a secret they do not personally own (§2.3)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    admin = await _make_user(session, tenant_a, "admin@x.test")

    ref = await _secrets_service(
        session, tenant_id=tenant_a, owner_id=user_a
    ).store_secret(name="a-secret", kind=SecretKind.MCP_AUTH, plaintext="value-admin-reads")

    svc_admin = _secrets_service(
        session, tenant_id=tenant_a, owner_id=admin, roles=(Role.MEMBER, Role.ADMIN)
    )
    assert await svc_admin.get_secret_plaintext(ref.id) == "value-admin-reads"
    await svc_admin.delete_secret(ref.id)
    assert await SecretRepository(session, tenant_a).get(ref.id) is None


async def test_get_nonexistent_secret_is_404(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A random, non-existent secret id → 404 (indistinguishable from non-owned)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)
    with pytest.raises(NotFoundError):
        await svc.get_secret_plaintext(uuid.uuid4())


async def test_repository_lookup_by_owner_name_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The adapter's ``get_by_owner_name`` handle resolves in-tenant, ``None`` across.

    An in-process adapter looks a credential up by its stable ``(owner, name)``
    handle. The lookup is tenant-scoped (INV-1): the same handle in another tenant
    resolves to ``None`` (never another tenant's secret).
    """
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    ref = await _secrets_service(
        session, tenant_id=tenant_a, owner_id=user_a
    ).store_secret(name="mcp", kind=SecretKind.MCP_AUTH, plaintext="handle-value-1234")

    in_tenant = await SecretRepository(session, tenant_a).get_by_owner_name(
        owner_id=user_a, name="mcp"
    )
    assert in_tenant is not None and in_tenant.id == ref.id
    # A tenant-B repository never resolves tenant-A's (owner, name) handle.
    assert (
        await SecretRepository(session, tenant_b).get_by_owner_name(owner_id=user_a, name="mcp")
        is None
    )


# ---------------------------------------------------------------------------
# AC-5: create / access / delete audited (INV-6); accessed names the reader.
# ---------------------------------------------------------------------------


async def test_store_emits_secret_created_audit(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-5/INV-6: a store emits ``secret.created`` (metadata only, not the value)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    await svc.store_secret(name="k", kind=SecretKind.MCP_AUTH, plaintext="value-secret-1234")

    events = await AuditEventRepository(session, tenant_a).list_recent(limit=10)
    created = [e for e in events if e.action == AuditAction.SECRET_CREATED.value]
    assert len(created) == 1
    # The audit metadata never carries the plaintext.
    meta = created[0].metadata
    assert "value-secret-1234" not in str(meta)
    assert meta.get("hint") == "****1234"


async def test_access_emits_secret_accessed_naming_the_reader(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-5/INV-6: an internal read emits ``secret.accessed`` recording who read it.

    The adapter/system reads on the platform's behalf; the audit records the
    *reader* (here the system actor), never the value.
    """
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    ref = await svc.store_secret(name="k", kind=SecretKind.SEARCH_API, plaintext="read-me-5678")
    # An adapter reads it in-process on the system's behalf.
    await svc.get_secret_plaintext(ref.id, accessor=AuditActor.system())

    events = await AuditEventRepository(session, tenant_a).list_recent(limit=10)
    accessed = [e for e in events if e.action == AuditAction.SECRET_ACCESSED.value]
    assert len(accessed) == 1
    # Recorded as a system read; the value is not in the event.
    assert accessed[0].actor_id is None
    assert "read-me-5678" not in str(accessed[0].metadata)


async def test_delete_emits_secret_deleted_audit(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """AC-5/INV-6: a delete emits ``secret.deleted`` and removes the row."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)

    ref = await svc.store_secret(name="k", kind=SecretKind.OTHER, plaintext="delete-me-0000")
    await svc.delete_secret(ref.id)

    assert AuditAction.SECRET_DELETED.value in await _audit_actions(session, tenant_a)
    assert await SecretRepository(session, tenant_a).get(ref.id) is None


async def test_denied_access_emits_permission_denied_audit(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-6: a denied read (non-owner) emits ``permission.denied``, not ``secret.accessed``."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")

    ref = await _secrets_service(
        session, tenant_id=tenant_a, owner_id=user_a
    ).store_secret(name="k", kind=SecretKind.MCP_AUTH, plaintext="a-value-9999")

    svc_b = _secrets_service(session, tenant_id=tenant_a, owner_id=user_b)
    with pytest.raises(NotFoundError):
        await svc_b.get_secret_plaintext(ref.id)

    actions = await _audit_actions(session, tenant_a)
    assert AuditAction.PERMISSION_DENIED.value in actions
    # A denied read must NOT be recorded as a successful access.
    denied_events = [
        e
        for e in await AuditEventRepository(session, tenant_a).list_recent(limit=20)
        if e.action == AuditAction.PERMISSION_DENIED.value
    ]
    assert denied_events and denied_events[0].outcome.value == "denied"


# ---------------------------------------------------------------------------
# INV-8: input validation at the boundary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["", "   "])
async def test_blank_name_is_rejected(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID], bad_name: str
) -> None:
    """INV-8: a blank secret name is a 422 — nothing is persisted."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)
    with pytest.raises(ValidationError):
        await svc.store_secret(name=bad_name, kind=SecretKind.MCP_AUTH, plaintext="v-1234")
    assert await SecretRepository(session, tenant_a).list_for_owner(user_a) == []


async def test_empty_value_is_rejected(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-8: an empty secret value is a 422 — nothing is persisted."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    svc = _secrets_service(session, tenant_id=tenant_a, owner_id=user_a)
    with pytest.raises(ValidationError):
        await svc.store_secret(name="k", kind=SecretKind.MCP_AUTH, plaintext="")
    assert await SecretRepository(session, tenant_a).list_for_owner(user_a) == []
