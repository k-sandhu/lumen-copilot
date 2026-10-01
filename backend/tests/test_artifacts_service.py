"""Artifact-store service — round-trip + the #208 negatives (INV-1/INV-2/INV-6).

Exercises ``app.services.artifacts_service.ArtifactsService`` end-to-end against
an **in-memory SQLite** schema (offline-safe, like the repository/grants tests)
with a **fake object store** that round-trips bytes in a dict keyed by
``(tenant_id, storage_key)`` and enforces the same artifact tenant-prefix seam the
real adapter does (:func:`assert_artifact_key_owned_by`). No MinIO, no Postgres.

Headlines:

* **AC-1** — ``create_artifact`` stores bytes + returns an ``Artifact``; the
  content round-trips via ``get_artifact_content`` and a presigned URL is minted.
* **AC-2** — over-cap / disallowed content-type → 422 (``ValidationError``), and
  **nothing is stored** (the reject happens before any write).
* **AC-3 (INV-1/INV-2)** — another tenant's / another owner's artifact id → the
  service returns ``None``/``False`` on get/content/presign/delete (the router's
  404), and a foreign artifact is never touched.
* **AC-4 (INV-6)** — create/download/delete emit ``artifact.created`` /
  ``.downloaded`` / ``.deleted`` through the one audit sink.
* **AC-5** — a forged cross-tenant artifact key is blocked by the store seam
  (``ForbiddenError``), asserted directly against the fake + real key helper.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import ForbiddenError, ValidationError
from app.db.base import Base
from app.db.repositories import (
    ArtifactRepository,
    AuditEventRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import ArtifactProducedBy, AuditEvent, Role
from app.services.artifacts_service import ArtifactLinks, ArtifactsService
from app.services.audit import AuditSink
from app.storage.keys import assert_artifact_key_owned_by, build_artifact_key
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip

_ALLOWED = frozenset({"text/csv", "application/json", "image/png"})
_MAX_BYTES = 1000


# ---------------------------------------------------------------------------
# A fake object store: round-trips bytes in a dict, enforces the artifact seam.
# ---------------------------------------------------------------------------


class _FakeStore:
    """No-network stand-in for ``ObjectStore``'s artifact methods (issue #208).

    Backs bytes in ``{(tenant, key): data}`` and enforces the **same** artifact
    tenant-prefix seam as the real adapter (``assert_artifact_key_owned_by``), so
    the AC-5 forged-key refusal is real here — not merely mocked away. Validation
    (allowlist/cap) is exercised through the *service*'s ``validate_upload`` call,
    so this fake stores whatever the service passes after that gate.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[str] = []

    async def put_artifact(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> object:
        from app.storage.object_store import StoredObject

        key = build_artifact_key(tenant_id, data, filename)
        self.objects[(tenant_id, key)] = data
        return StoredObject(
            key=key,
            sha256=key.split("/")[2],
            size_bytes=len(data),
            content_type=content_type,
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        assert_artifact_key_owned_by(key, tenant_id)  # the seam (AC-5)
        from app.core.errors import NotFoundError

        try:
            return self.objects[(tenant_id, key)]
        except KeyError as exc:
            raise NotFoundError("object not found", code="object_not_found") from exc

    async def delete_artifact(self, tenant_id: str, key: str) -> None:
        assert_artifact_key_owned_by(key, tenant_id)  # the seam (AC-5)
        self.objects.pop((tenant_id, key), None)
        self.deleted.append(key)

    async def presign_get_artifact(self, tenant_id: str, key: str) -> str:
        assert_artifact_key_owned_by(key, tenant_id)  # the seam (AC-5)
        return f"https://fake/{key}"


# ---------------------------------------------------------------------------
# Fixtures — in-memory SQLite schema + two tenants (mirrors the grants tests).
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


def _service(
    session: AsyncSession,
    store: _FakeStore,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    retention_days: int | None = None,
) -> ArtifactsService:
    audit = AuditSink(AuditEventRepository(session, tenant_id))
    return ArtifactsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        object_store=store,  # type: ignore[arg-type]  # structural fake
        audit=audit,
        denials=denial_context(RecordingDurableAuditTransactions(), session, tenant_id, owner_id),
        request_id="req-test",
        source_ip="203.0.113.1",
        artifact_allowed_content_types=_ALLOWED,
        max_artifact_bytes=_MAX_BYTES,
        retention_days=retention_days,
    )


async def _audit_actions(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    events: list[AuditEvent] = await AuditEventRepository(session, tenant_id).list_recent(limit=50)
    return [e.action for e in events]


# ---------------------------------------------------------------------------
# AC-1 — store + round-trip + presign.
# ---------------------------------------------------------------------------


async def test_create_stores_and_content_round_trips(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a)

    payload = b"col1,col2\n1,2\n"
    artifact = await svc.create_artifact(
        data=payload,
        filename="report.csv",
        content_type="text/csv",
        produced_by=ArtifactProducedBy.TOOL,
    )
    assert artifact.owner_id == user_a
    assert artifact.tenant_id == tenant_a
    assert artifact.size_bytes == len(payload)
    assert artifact.storage_key.startswith(f"artifacts/{tenant_a}/")
    assert artifact.retention_expires_at is None  # keep forever by default

    # Content round-trips (AC-1).
    content = await svc.get_artifact_content(artifact.id)
    assert content is not None
    assert content.data == payload
    assert content.mime_type == "text/csv"

    # A presigned GET URL is minted (AC-1).
    url = await svc.presign_artifact_content(artifact.id)
    assert url is not None
    assert artifact.storage_key in url


async def test_retention_window_is_stamped_when_configured(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a, retention_days=7)

    before = datetime.now(UTC)
    artifact = await svc.create_artifact(
        data=b'{"k": 1}',
        filename="out.json",
        content_type="application/json",
        produced_by=ArtifactProducedBy.RUN,
    )
    # A positive retention window stamps a future expiry ~7 days out. (Compared
    # against a tz-aware reference: SQLite round-trips created_at tz-naive, so we
    # don't compare the two persisted timestamps here — Postgres keeps both aware.)
    assert artifact.retention_expires_at is not None
    delta = artifact.retention_expires_at - before
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


async def test_list_is_owner_scoped_and_filterable(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    session_id = uuid.uuid4()
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a)

    await svc.create_artifact(
        data=b"a,b\n1,2\n",
        filename="one.csv",
        content_type="text/csv",
        produced_by=ArtifactProducedBy.TOOL,
    )
    await svc.create_artifact(
        data=b'{"x": 2}',
        filename="two.json",
        content_type="application/json",
        produced_by=ArtifactProducedBy.CHAT_SESSION,
        links=ArtifactLinks(session_id=session_id),
    )

    page = await svc.list_artifacts()
    assert len(page.items) == 2

    # produced_by filter narrows to one.
    only_tool = await svc.list_artifacts(produced_by=ArtifactProducedBy.TOOL)
    assert [a.filename for a in only_tool.items] == ["one.csv"]

    # session filter narrows to the chat-session artifact.
    by_session = await svc.list_artifacts(session_id=session_id)
    assert [a.filename for a in by_session.items] == ["two.json"]


# ---------------------------------------------------------------------------
# AC-2 — oversize / disallowed type → 422, nothing stored.
# ---------------------------------------------------------------------------


async def test_create_rejects_disallowed_type_before_storing(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a)

    with pytest.raises(ValidationError) as exc:
        await svc.create_artifact(
            data=b"MZ...",
            filename="evil.exe",
            content_type="application/x-msdownload",
            produced_by=ArtifactProducedBy.TOOL,
        )
    assert exc.value.status == 422
    # Nothing stored; no row; no audit.
    assert store.objects == {}
    assert await _audit_actions(session, tenant_a) == []
    assert (await svc.list_artifacts()).items == []


async def test_create_rejects_over_cap_before_storing(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a)

    with pytest.raises(ValidationError) as exc:
        await svc.create_artifact(
            data=b"x" * (_MAX_BYTES + 1),
            filename="big.csv",
            content_type="text/csv",
            produced_by=ArtifactProducedBy.TOOL,
        )
    assert exc.value.status == 422
    assert store.objects == {}


# ---------------------------------------------------------------------------
# AC-3 — cross-tenant / non-owned → 404 (None/False), foreign artifact untouched.
# ---------------------------------------------------------------------------


async def test_cross_tenant_artifact_is_not_found(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1: tenant B cannot get/download/presign/delete tenant A's artifact."""
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_b, "b@x.test")
    store = _FakeStore()

    svc_a = _service(session, store, tenant_id=tenant_a, owner_id=user_a)
    a_artifact = await svc_a.create_artifact(
        data=b"secret,data\n",
        filename="secret.csv",
        content_type="text/csv",
        produced_by=ArtifactProducedBy.TOOL,
    )

    svc_b = _service(session, store, tenant_id=tenant_b, owner_id=user_b)
    assert await svc_b.get_artifact(a_artifact.id) is None
    assert await svc_b.get_artifact_content(a_artifact.id) is None
    assert await svc_b.presign_artifact_content(a_artifact.id) is None
    assert await svc_b.delete_artifact(a_artifact.id) is False
    # A's artifact bytes were never touched by B's failed delete.
    assert (str(tenant_a), a_artifact.storage_key) in store.objects


async def test_other_owner_same_tenant_is_not_found(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-2: a same-tenant artifact owned by another user is 404 (not 403)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_c = await _make_user(session, tenant_a, "c@x.test")
    store = _FakeStore()

    svc_a = _service(session, store, tenant_id=tenant_a, owner_id=user_a)
    a_artifact = await svc_a.create_artifact(
        data=b"mine,only\n",
        filename="mine.csv",
        content_type="text/csv",
        produced_by=ArtifactProducedBy.TOOL,
    )

    # user_c is in the SAME tenant but is not the owner → invisible.
    svc_c = _service(session, store, tenant_id=tenant_a, owner_id=user_c)
    assert await svc_c.get_artifact(a_artifact.id) is None
    assert await svc_c.get_artifact_content(a_artifact.id) is None
    assert await svc_c.delete_artifact(a_artifact.id) is False
    # The owner still sees it.
    assert await svc_a.get_artifact(a_artifact.id) is not None


# ---------------------------------------------------------------------------
# AC-4 — create/download/delete emit audit events (INV-6).
# ---------------------------------------------------------------------------


async def test_lifecycle_emits_audit_events(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a)

    artifact = await svc.create_artifact(
        data=b"x,y\n1,2\n",
        filename="a.csv",
        content_type="text/csv",
        produced_by=ArtifactProducedBy.TOOL,
    )
    await svc.get_artifact_content(artifact.id)  # download
    assert await svc.delete_artifact(artifact.id) is True

    actions = await _audit_actions(session, tenant_a)
    assert AuditAction.ARTIFACT_CREATED.value in actions
    assert AuditAction.ARTIFACT_DOWNLOADED.value in actions
    assert AuditAction.ARTIFACT_DELETED.value in actions


async def test_presign_emits_download_audit(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a)

    artifact = await svc.create_artifact(
        data=b"x,y\n1,2\n",
        filename="a.csv",
        content_type="text/csv",
        produced_by=ArtifactProducedBy.TOOL,
    )
    await svc.presign_artifact_content(artifact.id)
    actions = await _audit_actions(session, tenant_a)
    # Presigning is a download too (the bytes leave storage).
    assert actions.count(AuditAction.ARTIFACT_DOWNLOADED.value) == 1


async def test_delete_removes_object_and_row(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    store = _FakeStore()
    svc = _service(session, store, tenant_id=tenant_a, owner_id=user_a)

    artifact = await svc.create_artifact(
        data=b"x,y\n1,2\n",
        filename="a.csv",
        content_type="text/csv",
        produced_by=ArtifactProducedBy.TOOL,
    )
    assert await svc.delete_artifact(artifact.id) is True
    # Object gone from the store and the row gone from the repo.
    assert (str(tenant_a), artifact.storage_key) not in store.objects
    assert await ArtifactRepository(session, tenant_a).get(artifact.id) is None
    # Idempotent-ish: a second delete is a 404 (already gone).
    assert await svc.delete_artifact(artifact.id) is False


# ---------------------------------------------------------------------------
# AC-5 — a forged cross-tenant artifact key is blocked at the store seam.
# ---------------------------------------------------------------------------


async def test_forged_cross_tenant_key_is_blocked_by_store_seam() -> None:
    """AC-5 (negative): the store refuses a key outside the caller's artifact prefix."""
    store = _FakeStore()
    # A key legitimately built for tenant-b, presented by a tenant-a caller.
    forged = build_artifact_key("tenant-b", b"secret", "leak.csv")
    with pytest.raises(ForbiddenError):
        await store.get_artifact("tenant-a", forged)
    with pytest.raises(ForbiddenError):
        await store.delete_artifact("tenant-a", forged)
    with pytest.raises(ForbiddenError):
        await store.presign_get_artifact("tenant-a", forged)
