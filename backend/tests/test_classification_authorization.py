"""INV-1/INV-2/INV-5/INV-6 at the internal classification service seam."""

from uuid import uuid4

import pytest

import app.db.session as db_session
from app.auth.principal import Principal
from app.core.errors import ForbiddenError, NotFoundError
from app.db.classification import ClassificationRepository
from app.db.repositories import (
    AuditEventRepository,
    DocumentRepository,
    GrantRepository,
    UserRepository,
)
from app.domain.entities import GrantPrincipalType, GrantResourceType, GrantRole, Role
from app.services.audit import AuditSink, PermissionDeniedContext
from app.services.classification_service import ClassificationService
from tests import test_ingestion_task as fixtures

sqlite_engine = fixtures.sqlite_engine


class Recorder:
    def __init__(self, tenant):
        self.tenant_id = tenant
        self.events = []

    async def emit(self, **kwargs):
        self.events.append(kwargs)


def service(session, principal):
    recorder = Recorder(principal.tenant_id)
    denials = PermissionDeniedContext(
        recorder, principal=principal, request_id="synthetic", source_ip="unknown"
    )
    return ClassificationService(
        session,
        principal=principal,
        audit=AuditSink(AuditEventRepository(session, principal.tenant_id)),
        denials=denials,
    ), recorder


async def test_foreign_and_invisible_are_404_even_for_admin(sqlite_engine):
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    for tid, roles in [(tenant, (Role.MEMBER,)), (tenant, (Role.ADMIN,)), (uuid4(), (Role.ADMIN,))]:
        async with db_session.tenant_session_scope(tid) as s:
            svc, recorder = service(s, Principal(uuid4(), tid, roles))
            with pytest.raises(NotFoundError):
                await svc.get(document)
            assert len(recorder.events) == 1


async def test_visible_reader_cannot_override_and_owner_can(sqlite_engine):
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as s:
        doc = await DocumentRepository(s, tenant).get(document)
        reader = await UserRepository(s, tenant).create(
            email="reader@example.test", password_hash="synthetic", roles=[Role.MEMBER]
        )
        await GrantRepository(s, tenant).create(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=document,
            principal_type=GrantPrincipalType.USER,
            principal_id=reader.id,
            role=GrantRole.VIEWER,
            granted_by=doc.owner_id,
        )
        repo = ClassificationRepository(s, tenant)
        await repo.schedule(
            document,
            extraction_id="a" * 64,
            input_json='{"text":"synthetic"}',
            taxonomy_version="1.0.0",
            controls={"enabled": False},
        )
        svc, recorder = service(s, Principal(reader.id, tenant, (Role.MEMBER,)))
        with pytest.raises(ForbiddenError):
            await svc.override(
                document,
                path="other/other/other",
                reason="synthetic",
                expected_revision=0,
                taxonomy_version="1.0.0",
            )
        assert len(recorder.events) == 1
        svc, _ = service(s, Principal(doc.owner_id, tenant, (Role.MEMBER,)))
        await svc.override(
            document,
            path="other/other/other",
            reason="synthetic",
            expected_revision=0,
            taxonomy_version="1.0.0",
        )
        work = await repo.get(document)
        assert work.override_path == "other/other/other"


async def test_failed_override_audit_rolls_back_override(sqlite_engine, monkeypatch):
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as s:
        doc = await DocumentRepository(s, tenant).get(document)
        await ClassificationRepository(s, tenant).schedule(
            document,
            extraction_id="a" * 64,
            input_json='{"text":"synthetic"}',
            taxonomy_version="1.0.0",
            controls={"enabled": False},
        )

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    with pytest.raises(RuntimeError):
        async with db_session.tenant_session_scope(tenant) as s:
            svc, _ = service(s, Principal(doc.owner_id, tenant, (Role.MEMBER,)))
            monkeypatch.setattr(svc._audit, "emit", broken)
            await svc.override(
                document,
                path="other/other/other",
                reason="synthetic",
                expected_revision=0,
                taxonomy_version="1.0.0",
            )
    async with db_session.tenant_session_scope(tenant) as s:
        assert (await ClassificationRepository(s, tenant).get(document)).override_path is None
