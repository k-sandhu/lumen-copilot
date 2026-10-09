"""Terminal service denial boundary: return/raise parity, isolation and exactly once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.auth.principal import Principal
from app.core.config import get_settings
from app.core.errors import ForbiddenError, NotFoundError
from app.domain.entities import Role
from app.services import audit
from app.services.assistants_service import AssistantsService
from app.services.connector_oauth_service import ConnectorOAuthService
from app.services.groups_service import GroupsService
from app.services.llm_providers_service import LlmProviderService
from app.services.mcp_servers_service import McpServersService
from app.services.runs_service import RunsControlService
from app.services.schedules_service import SchedulesService
from app.services.sources_service import SourcesService


@pytest.fixture
def context():
    rows = []
    tenant_id, user_id = uuid4(), uuid4()

    async def emit(**fields):
        rows.append(fields)

    denials = audit.PermissionDeniedContext(
        SimpleNamespace(tenant_id=tenant_id, emit=emit),
        principal=Principal(user_id, tenant_id, (Role.MEMBER,)),
        request_id="terminal",
        source_ip="unknown",
    )
    return denials, rows


@pytest.mark.parametrize("outcome", [None, False, NotFoundError, ForbiddenError])
async def test_every_terminal_outcome_passes_the_one_helper(context, outcome):
    denials, rows = context
    guard = getattr(audit, "audited_resource", None)
    assert guard is not None, "the common terminal denial helper is missing"

    class Service:
        _denials = denials

        @guard("resource.update", "resource", "resource_id", missing_result=True)
        async def update(self, resource_id):
            if isinstance(outcome, type):
                raise outcome("Missing or forbidden")
            return outcome

    resource_id = uuid4()
    if isinstance(outcome, type):
        with pytest.raises(outcome, match="Missing or forbidden"):
            await Service().update(resource_id)
    else:
        assert await Service().update(resource_id) is outcome
    assert len(rows) == 1
    assert rows[0]["resource_id"] == str(resource_id)
    assert rows[0]["attempted_action"] == "resource.update"


async def test_initial_guard_and_nested_terminal_helpers_emit_once(context):
    denials, rows = context
    guard = getattr(audit, "audited_resource", None)
    assert guard is not None

    class Service:
        _denials = denials

        @guard("resource.delete", "resource", "resource_id", missing_result=True)
        async def delete(self, resource_id):
            return await self.inner(resource_id)

        @guard("resource.read", "resource", "resource_id", missing_result=True)
        async def inner(self, resource_id):
            await denials.emit(
                resource_type="related",
                resource_id="related-id",
                attempted_action="resource.delete",
                reason="not_visible",
            )
            return False

    assert await Service().delete(uuid4()) is False
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "related"


async def test_nested_services_with_separately_built_request_contexts_emit_once(context):
    """The chat builder constructs separate contexts for chat and sandbox services."""
    denials, rows = context
    nested = audit.PermissionDeniedContext(
        denials._recorder,
        principal=Principal(denials.require_user(), denials.tenant_id, (Role.MEMBER,)),
        request_id=denials.request_id,
        source_ip=denials.source_ip,
    )

    class Inner:
        _denials = nested

        @audit.audited_resource("resource.close", "resource", "resource_id", missing_result=True)
        async def close(self, resource_id):
            return False

    class Outer:
        _denials = denials

        @audit.audited_resource("resource.delete", "resource", "resource_id", missing_result=True)
        async def delete(self, resource_id):
            return await Inner().close(resource_id)

    assert await Outer().delete(uuid4()) is False
    assert len(rows) == 1


async def test_successful_none_and_zero_are_not_missing_outcomes(context):
    denials, rows = context
    guard = getattr(audit, "audited_resource", None)
    assert guard is not None

    class Service:
        _denials = denials

        @guard("resource.close", "resource", "resource_id")
        async def close(self, resource_id):
            return None

        @guard("resource.count", "resource", "resource_id", missing_result=True)
        async def count(self, resource_id):
            return 0

    assert await Service().close(uuid4()) is None
    assert await Service().count(uuid4()) == 0
    assert rows == []


async def test_parallel_attempts_do_not_share_recorded_state(context):
    denials, rows = context
    guard = getattr(audit, "audited_resource", None)
    assert guard is not None
    entered, resume = asyncio.Event(), asyncio.Event()

    class Service:
        _denials = denials

        @guard("resource.read", "resource", "resource_id", missing_result=True)
        async def get(self, resource_id):
            if resource_id == "first":
                entered.set()
                await resume.wait()
            return None

    task = asyncio.create_task(Service().get("first"))
    await entered.wait()
    try:
        assert await Service().get("second") is None
    finally:
        resume.set()
        await task
    assert sorted(row["resource_id"] for row in rows) == ["first", "second"]


async def test_terminal_sink_failure_propagates(context):
    denials, rows = context
    guard = getattr(audit, "audited_resource", None)
    assert guard is not None

    async def fail(**fields):
        raise RuntimeError("audit unavailable")

    denials._recorder.emit = fail

    class Service:
        _denials = denials

        @guard("resource.read", "resource", "resource_id", missing_result=True)
        async def get(self, resource_id):
            return None

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await Service().get(uuid4())
    assert rows == []


@pytest.mark.parametrize(
    "owner,method,action",
    [
        (SchedulesService, "pause", "schedule.pause"),
        (SchedulesService, "resume", "schedule.resume"),
        (SchedulesService, "delete", "schedule.delete"),
        (AssistantsService, "delete", "assistant.delete"),
        (GroupsService, "delete_group", "group.delete"),
        (RunsControlService, "resume", "run.resume"),
        (RunsControlService, "cancel", "run.cancel"),
        (RunsControlService, "reroute", "run.reroute"),
        (SourcesService, "resync", "source.sync"),
        (McpServersService, "test", "mcp_server.test"),
    ],
)
async def test_late_empty_mutation_never_asserts_or_audits_success(
    context, monkeypatch, owner, method, action
):
    """A real service receives a missing mutation after its successful guard.

    This is an adapter-result test of the additional RETURNING-empty branches;
    the HTTP/database interleaving itself is covered by test_audit_r7_live.
    """
    denials, rows = context
    target = SimpleNamespace(
        id=uuid4(),
        assistant_id=uuid4(),
        name="owned",
        owner_id=uuid4(),
        status="ready",
        type="web",
        enabled=method != "resume",
        cadence=None,
        timezone="UTC",
    )
    repo = SimpleNamespace(
        update=AsyncMock(return_value=None),
        delete=AsyncMock(return_value=False),
        mark_queued=AsyncMock(return_value=None),
        mark_terminal=AsyncMock(return_value=None),
        reassign_owner=AsyncMock(),
        update_status=AsyncMock(return_value=None),
        update_health=AsyncMock(return_value=None),
    )
    success = AsyncMock()
    service = SimpleNamespace(
        _denials=denials,
        _schedules=repo,
        _assistants=repo,
        _groups=repo,
        _runs=repo,
        _sources=repo,
        _servers=repo,
        _users=SimpleNamespace(get=AsyncMock(return_value=object())),
        _load_managed_or_404=AsyncMock(return_value=target),
        _get_or_404=AsyncMock(return_value=target),
        _load_escalated=AsyncMock(return_value=target),
        _visible=AsyncMock(return_value=target),
        _require_admin=AsyncMock(),
        _reject_if_system=lambda group: None,
        _is_managed=lambda kind: False,
        _emit=success,
        _emit_audit=success,
        _audit=SimpleNamespace(emit=success),
        _projector=SimpleNamespace(remove=lambda identifier: None, sync=lambda row: None),
        _config_for=lambda server: None,
        _build_client=lambda: SimpleNamespace(
            health=AsyncMock(return_value=SimpleNamespace(ok=True)),
            list_tools=AsyncMock(return_value=[]),
        ),
        _request_id="terminal",
        _source_ip="unknown",
        _owner_id=denials.require_user(),
        _embedding_space_fingerprint=get_settings().embedding_space_fingerprint,
    )
    monkeypatch.setattr("app.services.schedules_service.compute_next_run", lambda *args: None)
    kwargs = {"to_owner_id": uuid4()} if method == "reroute" else {}
    if owner in (SourcesService, McpServersService):
        assert await getattr(owner, method)(service, target.id, **kwargs) is None
    else:
        with pytest.raises(NotFoundError):
            await getattr(owner, method)(service, target.id, **kwargs)
    assert success.await_count == 0
    assert len(rows) == 1
    assert rows[0]["attempted_action"] == action


async def test_provider_refresh_empty_returning_is_a_terminal_denial(context):
    denials, rows = context
    provider = SimpleNamespace(id=uuid4(), api_key_secret_ref=None, base_url="https://test.invalid")
    service = SimpleNamespace(
        _denials=denials,
        _require_admin=AsyncMock(),
        _visible=AsyncMock(return_value=provider),
        _resolve_api_key=AsyncMock(return_value=None),
        _discover_models=AsyncMock(return_value=[]),
        _providers=SimpleNamespace(set_discovery=AsyncMock(return_value=None)),
    )
    service._run_discovery = lambda value: LlmProviderService._run_discovery(service, value)
    with pytest.raises(NotFoundError):
        await LlmProviderService.refresh(service, provider.id)
    assert len(rows) == 1
    assert rows[0]["attempted_action"] == "llm_provider.refresh"


async def test_oauth_start_empty_returning_is_a_terminal_denial(context, monkeypatch):
    denials, rows = context
    source = SimpleNamespace(id=uuid4(), type="gdrive", status="ready")
    repository = SimpleNamespace(
        get=AsyncMock(return_value=source), begin_connect=AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.services.connector_oauth_service.SourceRepository", lambda *args: repository
    )
    service = SimpleNamespace(_session=None, _oauth_connector=lambda kind: (None, None))
    principal = Principal(denials.require_user(), denials.tenant_id, (Role.ADMIN,))
    with pytest.raises(NotFoundError):
        await ConnectorOAuthService.start_connect(
            service, source.id, principal=principal, denials=denials
        )
    assert len(rows) == 1
    assert rows[0]["attempted_action"] == "source.connect"
