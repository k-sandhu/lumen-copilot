"""The ``write_file`` tool — the agent file-writing capability (issue #220).

Drives :mod:`app.services.tools.impls.write_file` **offline** (in-memory SQLite +
a fake object store, exactly like ``test_artifacts_service.py``) so no MinIO /
Postgres is needed, and asserts the whole #220 contract negative-first:

* **AC-1** — a text write persists a real ``Artifact`` through the #208 service
  and returns a downloadable ref; the bytes round-trip. Base64 encoding writes a
  binary artifact whose bytes round-trip too.
* **AC-2** — an over-cap payload / a disallowed content-type / a malformed base64
  body / a missing filename become an ``ok=False`` handler result — never a crash.
* **AC-3 (INV-1/INV-2)** — tenant/owner isolation is preserved by delegation: the
  handler calls ``create_artifact`` on the service scoped to the *caller's*
  principal (owner/tenant come from the service, never the tool args). The
  cross-tenant fetch 404 is CC-B's, already covered in ``test_artifacts_service``.
* **AC-4 (INV-6)** — a write emits ``artifact.created`` through the one audit sink;
  read-only test mode (``simulate_writes``) returns success but persists **nothing**
  and emits no audit event (the negative).

The tool is registered and governed: it is discoverable, **T1**, and **not**
read-only, so it is absent from the default ad-hoc allow-list (a deliberate
allow-list decision, deny-by-default preserved).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Artifact, ArtifactProducedBy, AuditEvent, Role
from app.domain.tools import ERROR_BAD_ARGS, RiskTier, ToolHandlerResult
from app.services.artifacts_service import ArtifactLinks, ArtifactsService
from app.services.audit import AuditSink
from app.services.tools import registry
from app.services.tools.impls.write_file import _write_file
from app.services.tools.types import ToolContext
from app.storage.keys import assert_artifact_key_owned_by, build_artifact_key

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip

# ``asyncio_mode = auto`` (pyproject) runs the async tests; the one sync test
# (registration/governance) stays sync — no module-level asyncio mark.

_ALLOWED = frozenset({"text/csv", "application/json", "text/markdown", "text/plain", "image/png"})
_MAX_BYTES = 1000


# ---------------------------------------------------------------------------
# A fake object store (mirrors test_artifacts_service): round-trips bytes and
# enforces the same tenant-prefix seam the real adapter does.
# ---------------------------------------------------------------------------


class _FakeStore:
    """No-network stand-in for ``ObjectStore``'s artifact methods (issue #208)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put_artifact(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> object:
        from app.storage.object_store import StoredObject

        key = build_artifact_key(tenant_id, data, filename)
        self.objects[(tenant_id, key)] = data
        return StoredObject(
            key=key, sha256=key.split("/")[2], size_bytes=len(data), content_type=content_type
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        assert_artifact_key_owned_by(key, tenant_id)
        return self.objects[(tenant_id, key)]

    async def presign_get_artifact(self, tenant_id: str, key: str) -> str:
        assert_artifact_key_owned_by(key, tenant_id)
        return f"https://fake/{key}"

    async def delete_artifact(self, tenant_id: str, key: str) -> None:  # pragma: no cover
        self.objects.pop((tenant_id, key), None)


# ---------------------------------------------------------------------------
# Fixtures — in-memory SQLite schema + one tenant/user.
# ---------------------------------------------------------------------------


class _World:
    def __init__(
        self, *, session: AsyncSession, store: _FakeStore, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        self.session = session
        self.store = store
        self.tenant_id = tenant_id
        self.user_id = user_id

    @property
    def principal(self) -> Principal:
        return Principal(user_id=self.user_id, tenant_id=self.tenant_id, roles=(Role.MEMBER,))

    def service(self) -> ArtifactsService:
        audit = AuditSink(AuditEventRepository(self.session, self.tenant_id))
        return ArtifactsService(
            self.session,
            tenant_id=self.tenant_id,
            owner_id=self.user_id,
            object_store=self.store,  # type: ignore[arg-type]  # structural fake
            audit=audit,
            request_id="req-test",
            source_ip="203.0.113.1",
            artifact_allowed_content_types=_ALLOWED,
            max_artifact_bytes=_MAX_BYTES,
        )

    def context(self, *, simulate_writes: bool = False, with_writer: bool = True) -> ToolContext:
        return ToolContext(
            principal=self.principal,
            retrieval=object(),  # type: ignore[arg-type]  # write_file never touches retrieval
            artifacts=self.service() if with_writer else None,
            session_id=None,
            simulate_writes=simulate_writes,
        )


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Acme")
            user = await UserRepository(session, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            await session.commit()
            yield _World(
                session=session, store=_FakeStore(), tenant_id=tenant.id, user_id=user.id
            )
    finally:
        await engine.dispose()


async def _audit_actions(world: _World) -> list[str]:
    repo = AuditEventRepository(world.session, world.tenant_id)
    events: list[AuditEvent] = await repo.list_recent(limit=50)
    return [e.action for e in events]


# ---------------------------------------------------------------------------
# Registration / governance.
# ---------------------------------------------------------------------------


def test_write_file_is_registered_and_governed_as_t1() -> None:
    defn = registry.get_tool("write_file")
    assert defn.risk_tier is RiskTier.T1
    assert defn.read_only is False
    # Not read-only ⇒ absent from the default ad-hoc allow-list (deny-by-default).
    assert "write_file" not in registry.default_allowlist()
    # T1 does not require approval (only T2+ are gated).
    assert defn.requires_approval is False


# ---------------------------------------------------------------------------
# AC-1 — a real artifact is persisted and its bytes round-trip.
# ---------------------------------------------------------------------------


async def test_text_write_persists_and_round_trips(world: _World) -> None:
    svc = world.service()
    ctx = ToolContext(
        principal=world.principal,
        retrieval=object(),  # type: ignore[arg-type]
        artifacts=svc,
    )
    result = await _write_file(
        {"filename": "summary.md", "content_type": "text/markdown", "content": "# Title\n\nBody."},
        ctx,
    )
    assert result.ok is True
    artifact_id = result.payload["artifact_id"]
    assert result.payload["download_ref"] == f"/v1/artifacts/{artifact_id}/content"
    assert result.payload["content_type"] == "text/markdown"
    assert artifact_id in result.content  # the model sees the id + a download ref

    # The bytes round-trip through the same service (AC-1).
    content = await svc.get_artifact_content(uuid.UUID(artifact_id))
    assert content is not None
    assert content.data == b"# Title\n\nBody."
    assert content.filename == "summary.md"


async def test_base64_write_round_trips_binary(world: _World) -> None:
    svc = world.service()
    ctx = ToolContext(
        principal=world.principal, retrieval=object(), artifacts=svc  # type: ignore[arg-type]
    )
    raw = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03"
    result = await _write_file(
        {
            "filename": "chart.png",
            "content_type": "image/png",
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        },
        ctx,
    )
    assert result.ok is True
    content = await svc.get_artifact_content(uuid.UUID(result.payload["artifact_id"]))
    assert content is not None
    assert content.data == raw


# ---------------------------------------------------------------------------
# AC-2 — violations are tool errors, never crashes.
# ---------------------------------------------------------------------------


async def test_oversize_payload_is_ok_false_not_crash(world: _World) -> None:
    ctx = world.context()
    big = "x" * (_MAX_BYTES + 1)
    result = await _write_file(
        {"filename": "big.txt", "content_type": "text/plain", "content": big}, ctx
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    # Nothing was stored (the reject happens inside the service before any write).
    assert world.store.objects == {}


async def test_disallowed_content_type_is_ok_false(world: _World) -> None:
    ctx = world.context()
    result = await _write_file(
        {"filename": "app.exe", "content_type": "application/x-msdownload", "content": "MZ"}, ctx
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert world.store.objects == {}


async def test_malformed_base64_is_ok_false(world: _World) -> None:
    ctx = world.context()
    result = await _write_file(
        {
            "filename": "x.png",
            "content_type": "image/png",
            "content": "not-valid-base64!!!",
            "encoding": "base64",
        },
        ctx,
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert world.store.objects == {}


async def test_missing_filename_is_ok_false(world: _World) -> None:
    ctx = world.context()
    result = await _write_file({"content_type": "text/plain", "content": "hi"}, ctx)
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert world.store.objects == {}


async def test_non_string_content_is_ok_false(world: _World) -> None:
    ctx = world.context()
    result = await _write_file(
        {"filename": "x.txt", "content_type": "text/plain", "content": 42}, ctx
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS


async def test_bad_encoding_is_ok_false(world: _World) -> None:
    ctx = world.context()
    result = await _write_file(
        {"filename": "x.txt", "content_type": "text/plain", "content": "hi", "encoding": "rot13"},
        ctx,
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS


async def test_no_writer_wired_is_ok_false_not_crash(world: _World) -> None:
    ctx = world.context(with_writer=False)
    result = await _write_file(
        {"filename": "x.txt", "content_type": "text/plain", "content": "hi"}, ctx
    )
    assert result.ok is False  # a run with no write seam reports, never crashes


# ---------------------------------------------------------------------------
# AC-3 (INV-1/INV-2) — isolation is preserved by delegating to CC-B with the
# caller's principal (owner/tenant never come from the tool args).
# ---------------------------------------------------------------------------


class _SpyWriter:
    """Records the ``create_artifact`` call so we can assert the delegation shape."""

    def __init__(self, artifact: Artifact) -> None:
        self._artifact = artifact
        self.calls: list[dict[str, object]] = []

    async def create_artifact(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        produced_by: ArtifactProducedBy,
        links: ArtifactLinks | None = None,
    ) -> Artifact:
        self.calls.append(
            {
                "data": data,
                "filename": filename,
                "content_type": content_type,
                "produced_by": produced_by,
                "links": links,
            }
        )
        return self._artifact


async def test_delegates_to_service_with_tool_origin(world: _World) -> None:
    # The tool supplies only bytes + metadata; owner/tenant are the service's (from
    # the principal), so a tool arg can never redirect the write to another tenant.
    session_id = uuid.uuid4()
    artifact = Artifact(
        id=uuid.uuid4(),
        tenant_id=world.tenant_id,
        owner_id=world.user_id,
        produced_by=ArtifactProducedBy.TOOL,
        filename="d.csv",
        mime_type="text/csv",
        size_bytes=3,
        storage_key="artifacts/x/y/d.csv",
        sha256="y",
        created_at=datetime.now(UTC),
    )
    spy = _SpyWriter(artifact)
    ctx = ToolContext(
        principal=world.principal,
        retrieval=object(),  # type: ignore[arg-type]
        artifacts=spy,
        session_id=session_id,
    )
    result = await _write_file(
        {"filename": "d.csv", "content_type": "text/csv", "content": "a,b\n1,2\n"}, ctx
    )
    assert result.ok is True
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["produced_by"] is ArtifactProducedBy.TOOL
    assert call["data"] == b"a,b\n1,2\n"
    # The producing session is threaded through so the artifact links back (issue #208).
    assert isinstance(call["links"], ArtifactLinks)
    assert call["links"].session_id == session_id
    # The write is not parameterised by any owner/tenant arg — those are the
    # service's, resolved from the principal (INV-1/INV-2 by delegation).
    assert "owner_id" not in call
    assert "tenant_id" not in call


# ---------------------------------------------------------------------------
# AC-4 (INV-6) — the write is audited; read-only test mode simulates (no persist).
# ---------------------------------------------------------------------------


async def test_write_is_audited(world: _World) -> None:
    ctx = world.context()
    result = await _write_file(
        {"filename": "r.json", "content_type": "application/json", "content": '{"ok": true}'}, ctx
    )
    assert result.ok is True
    assert "artifact.created" in await _audit_actions(world)


async def test_read_only_mode_simulates_without_persisting(world: _World) -> None:
    ctx = world.context(simulate_writes=True)
    result = await _write_file(
        {"filename": "r.json", "content_type": "application/json", "content": '{"ok": true}'}, ctx
    )
    # The action succeeds (the model sees a result) but nothing is persisted...
    assert result.ok is True
    assert result.payload["simulated"] is True
    assert isinstance(result, ToolHandlerResult)
    assert world.store.objects == {}
    # ...and no artifact.created audit event fired (the AC-4 negative).
    assert "artifact.created" not in await _audit_actions(world)
