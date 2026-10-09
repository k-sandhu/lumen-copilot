"""R4-003 trusted tenant/principal construction and direct-service invariants."""

import inspect
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.api.deps import AuditSinkFactoryValue, authenticated_denial_context
from app.auth.principal import Principal
from app.domain.audit import AuditActor
from app.services.audit import PermissionDeniedContext
from app.services.saved_searches_service import SavedSearchService
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_recorder
from tests.test_denial_guard_registry import _OWNER_SEAMS


def test_api_factory_rejects_foreign_principal_before_recording() -> None:
    ledger = RecordingDurableAuditTransactions()
    factory = AuditSinkFactoryValue(Mock(), ledger)
    with pytest.raises(ValueError, match="tenant"):
        authenticated_denial_context(
            factory,
            tenant_id=uuid4(),
            principal=Principal(uuid4(), uuid4(), ()),
            request=Request({"type": "http", "headers": []}),
        )
    assert ledger.events == []


def test_context_rejects_foreign_principal_and_unverified_user_actor() -> None:
    ledger = RecordingDurableAuditTransactions()
    recorder = denial_recorder(ledger, Mock(), uuid4())
    with pytest.raises(ValueError, match="tenant"):
        PermissionDeniedContext(
            recorder,
            principal=Principal(uuid4(), uuid4(), ()),
            request_id="R4-003",
            source_ip="unknown",
        )
    with pytest.raises(ValueError, match="principal"):
        PermissionDeniedContext(
            recorder, actor=AuditActor.user(uuid4()), request_id="R4-003", source_ip="unknown"
        )
    assert ledger.events == []


@pytest.mark.parametrize(
    "actor", [AuditActor(None), AuditActor(uuid4(), is_system=True), AuditActor(None, True, True)]
)
def test_context_rejects_invalid_actor_kind(actor: AuditActor) -> None:
    ledger = RecordingDurableAuditTransactions()
    with pytest.raises(ValueError):
        PermissionDeniedContext(
            denial_recorder(ledger, Mock(), uuid4()),
            actor=actor,
            request_id="R4-003",
            source_ip="system",
        )
    assert ledger.events == []


async def test_valid_tenant_pairs_emit_once_and_foreign_service_fails_before_lookup(
    monkeypatch,
) -> None:
    ledger = RecordingDurableAuditTransactions()
    repository = Mock()
    for _ in range(2):
        tenant_id, user_id = uuid4(), uuid4()
        context = PermissionDeniedContext(
            denial_recorder(ledger, Mock(), tenant_id),
            principal=Principal(user_id, tenant_id, ()),
            request_id="R4-003",
            source_ip="unknown",
        )
        with pytest.raises(ValueError, match="tenant"):
            SavedSearchService(repository, tenant_id=uuid4(), owner_id=user_id, denials=context)
        repository.execute.assert_not_called()
        assert context.tenant_id == tenant_id
        fake_repository = Mock()
        fake_repository.get = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "app.services.saved_searches_service.SavedSearchRepository",
            lambda *args, repository=fake_repository: repository,
        )
        service = SavedSearchService(
            repository, tenant_id=tenant_id, owner_id=user_id, denials=context
        )
        assert await service.get(uuid4()) is None
        fake_repository.get.assert_awaited_once()
        assert ledger.events[-1].tenant_id == tenant_id
        assert ledger.events[-1].actor_id == user_id
    assert len(ledger.events) == 2


async def test_system_context_has_tenant_and_null_actor() -> None:
    ledger = RecordingDurableAuditTransactions()
    tenant_id = uuid4()
    context = PermissionDeniedContext(
        denial_recorder(ledger, Mock(), tenant_id),
        actor=AuditActor.system(),
        request_id="R4-003-system",
        source_ip="system",
    )
    await context.emit(
        resource_type="assistant",
        resource_id="hidden",
        attempted_action="run.enqueue",
        reason="not_visible",
    )
    assert len(ledger.events) == 1
    assert ledger.events[0].tenant_id == tenant_id
    assert ledger.events[0].actor_id is None


@pytest.mark.parametrize(
    "owner",
    [
        "AssistantsService",
        "AssistantGovernanceService",
        "AssistantTestService",
        "ChatService",
        "CollectionsService",
        "RunsReadService",
        "RunsControlService",
        "SchedulesService",
        "enqueue_manual_run",
        "SandboxReadService",
        "SandboxSessionService",
    ],
)
async def test_each_migrated_guard_rejects_foreign_context_before_lookup(owner: str) -> None:
    ledger = RecordingDurableAuditTransactions()
    trusted_tenant, service_tenant, user_id = uuid4(), uuid4(), uuid4()
    session = Mock()
    context = PermissionDeniedContext(
        denial_recorder(ledger, session, trusted_tenant),
        principal=Principal(user_id, trusted_tenant, ()),
        request_id="R4-003-pair",
        source_ip="unknown",
    )
    seam = _OWNER_SEAMS[owner]
    known = {
        "session": session,
        "tenant_id": service_tenant,
        "owner_id": user_id,
        "actor_id": user_id,
        "roles": (),
        "denials": context,
        "principal": Principal(user_id, service_tenant, ()),
    }
    kwargs = {
        name: known.get(name, Mock())
        for name, parameter in inspect.signature(seam).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    with pytest.raises(ValueError, match="tenant"):
        result = seam(**kwargs)
        if inspect.isawaitable(result):
            await result
    session.execute.assert_not_called()
    assert ledger.events == []
