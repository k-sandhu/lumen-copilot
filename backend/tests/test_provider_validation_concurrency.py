"""An in-flight probe cannot certify a concurrently changed provider (#585)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.auth.principal import Principal
from app.db.repositories import LlmProviderRepository
from app.domain.entities import LlmProviderStatus, Role
from app.services import llm_providers_service as svc
from app.services.audit import PermissionDeniedContext
from tests.test_provider_models import _provider


async def test_probe_lock_selects_only_its_tenant_and_provider() -> None:
    tenant_id, provider_id = uuid4(), uuid4()
    execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    repository = LlmProviderRepository(SimpleNamespace(execute=execute), tenant_id)
    assert await repository.get(provider_id, for_update=True) is None
    query = execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "FOR UPDATE" in str(query)
    assert "llm_providers.tenant_id =" in str(query)
    assert set(query.params.values()) == {tenant_id, provider_id}


async def test_config_edit_waits_for_probe_then_invalidates_its_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _provider(status=LlmProviderStatus.REACHABLE)
    row = replace(row, validation_checks=svc._discovery_checks(has_key=False, error=None))
    lock = asyncio.Lock()
    probe_started = asyncio.Event()
    edit_waiting = asyncio.Event()
    release_probe = asyncio.Event()

    async def get(provider_id, *, for_update=False):
        if for_update:
            if lock.locked():
                edit_waiting.set()
            await lock.acquire()
        return row

    async def persist(provider_id, **values):
        nonlocal row
        row = replace(row, **values, validation_grandfathered=False)
        return row

    async def update(provider_id, **values):
        nonlocal row
        row = replace(row, base_url=values["base_url"])
        return row

    service = SimpleNamespace(
        _denials=PermissionDeniedContext(
            SimpleNamespace(tenant_id=row.tenant_id, emit=AsyncMock()),
            principal=Principal(row.owner_id, row.tenant_id, (Role.ADMIN,)),
            request_id="probe-race",
            source_ip="unknown",
        ),
        _require_admin=AsyncMock(),
        _providers=SimpleNamespace(get=get, set_discovery=persist, update=update),
        _resolve_api_key=AsyncMock(return_value=None),
        _validate_base_url=lambda value: None,
        _discover_models=AsyncMock(return_value=row.discovered_models),
        _audit=SimpleNamespace(emit=AsyncMock()),
        _owner_id=row.owner_id,
        _request_id="probe-race",
        _source_ip="unknown",
        _discovery_timeout_seconds=5,
        _user_agent="test",
        _discovery_client=None,
    )
    service._visible = lambda *args, **kwargs: svc.LlmProviderService._visible(
        service, *args, **kwargs
    )
    service._run_discovery = lambda provider: svc.LlmProviderService._run_discovery(
        service, provider
    )

    async def probe(**kwargs):
        probe_started.set()
        await release_probe.wait()

    monkeypatch.setattr(svc, "probe_test_connection", probe)

    async def test_connection():
        try:
            return await svc.LlmProviderService.test_connection(service, row.id, model="m1")
        finally:
            # The route commits/releases its transaction after the service returns.
            lock.release()

    async def retarget():
        try:
            return await svc.LlmProviderService.update(
                service, row.id, base_url="https://new-provider.example/v1"
            )
        finally:
            lock.release()

    testing = asyncio.create_task(test_connection())
    started = asyncio.create_task(probe_started.wait())
    await asyncio.wait({testing, started}, return_when=asyncio.FIRST_COMPLETED)
    if testing.done():
        started.cancel()
        await asyncio.gather(started, return_exceptions=True)
        testing.result()
        raise AssertionError("Connection test did not reach the probe")
    await started
    editing = asyncio.create_task(retarget())
    waiting = asyncio.create_task(edit_waiting.wait())
    try:
        await asyncio.wait({editing, waiting}, return_when=asyncio.FIRST_COMPLETED)
        assert not editing.done()
    finally:
        release_probe.set()
        waiting.cancel()
        await asyncio.gather(testing, editing, waiting, return_exceptions=True)
    assert row.base_url == "https://new-provider.example/v1"
    assert row.status is LlmProviderStatus.REACHABLE
    assert (
        next(c for c in row.validation_checks if c["name"] == "auth_verified")["status"] != "passed"
    )
