"""MCP tools into the governed CC-A registry (#227, ADR-0012 §6, negative-first).

Discovered MCP tools become usable by an assistant under the SAME governance as
native tools: namespaced ``mcp:<slug>:<tool>``, risk-tiered, approval-gated, allow-
listed per assistant, tenant-scoped, and audited — through the one
:class:`~app.services.tools.runner.ToolRunner` chokepoint. These tests drive the
bridge (:mod:`app.services.tools.mcp_bridge`) + the runner + the per-run resolver
(:class:`~app.services.mcp_servers_service.McpServersService`) offline, against an
in-memory SQLite DB and an in-process FastMCP fixture server (no socket), and assert
the whole contract as negatives:

* **AC-1** — a registered+enabled server's discovered tools appear namespaced and,
  when the assistant allow-lists a *read-only* one, are invokable in a run (result
  flows through the runner: audited + a ``tool_invocations`` row).
* **AC-2** — a tool schema violation → a typed ``tool_bad_args`` result, no outbound
  call (malformed input rejected at the boundary, INV-8).
* **AC-3** — a write-capable (unannotated) MCP tool defaults to a write tier that
  ``requires_approval``; unapproved → NOT executed (``approval_denied``, INV-7).
* **allow-list** — an MCP tool NOT named in the allow-list → ``tool_not_permitted``;
  the handler never runs.
* **AC-4 / INV-1** — a disabled server's tools are never resolved/offered; another
  tenant's / another owner's server never contributes (cross-tenant isolation).
* **§7** — a downed server → an ``ok=False`` result, never a crash.
* **allow-list validation** — an assistant may name its own ``mcp:*`` tools; a
  bogus / cross-tenant ``mcp:*`` name → 422 ``unknown_tool``.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata
from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    McpServerRepository,
    TenantRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import McpServer, McpServerStatus, Role
from app.domain.llm import ToolCall
from app.domain.tools import (
    ERROR_APPROVAL_DENIED,
    ERROR_BAD_ARGS,
    ERROR_NOT_PERMITTED,
    RiskTier,
)
from app.mcp import MCP_ERROR_UNAVAILABLE, McpServerConfig, McpToolResult
from app.services.audit import AuditSink
from app.services.mcp_servers_service import build_mcp_servers_service
from app.services.mcp_servers_service import (
    build_transport_client_factory,
)
from app.services.tools import mcp_bridge
from app.services.tools.mcp_bridge import (
    namespaced_tool_name,
    slug_for_server,
    tools_for_servers,
)
from app.services.tools.runner import ToolRunner
from app.services.tools.types import ApprovalRequest, ToolContext
from tests._mcp_fixture_server import fixture_mcp

# --- world ------------------------------------------------------------------


class _FakeRetrieval:
    """A retrieval stand-in; these governance tests never actually search."""


class _World:
    def __init__(
        self, *, session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id


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
            yield _World(session=session, tenant_id=tenant.id, user_id=user.id)
    finally:
        await engine.dispose()


def _principal(w: _World) -> Principal:
    return Principal(user_id=w.user_id, tenant_id=w.tenant_id, roles=(Role.MEMBER,))


def _context(w: _World) -> ToolContext:
    return ToolContext(principal=_principal(w), retrieval=_FakeRetrieval())  # type: ignore[arg-type]


def _make_runner(
    w: _World,
    *,
    allowed: frozenset[str],
    extra_tools: dict[str, Any],
    gate: Any | None = None,
) -> tuple[ToolRunner, AuditEventRepository]:
    audit_repo = AuditEventRepository(w.session, w.tenant_id)
    r = ToolRunner(
        allowed=allowed,
        invocations=ToolInvocationRepository(w.session, w.tenant_id),
        audit=AuditSink(audit_repo),
        actor=AuditActor.user(w.user_id),
        request_id="req-1",
        source_ip="127.0.0.1",
        session_id=None,
        gate=gate,
        extra_tools=extra_tools,
    )
    return r, audit_repo


async def _all_invocations(w: _World) -> list[Any]:
    from sqlalchemy import select

    from app.db import models

    stmt = select(models.ToolInvocation).where(
        models.ToolInvocation.tenant_id == w.tenant_id
    )
    return list((await w.session.execute(stmt)).scalars().all())


# --- fake servers + invokers ------------------------------------------------


def _server(
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    enabled: bool = True,
    tools: list[dict[str, object]] | None = None,
) -> McpServer:
    """A bare :class:`McpServer` row with a discovered-tools snapshot (no DB needed).

    The bridge is pure over the passed rows, so a hand-built entity exercises it
    without a live server — the fixture-server round-trip is a separate test.
    """
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    return McpServer(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        owner_id=owner_id,
        name="fixture",
        transport="streamable_http",
        endpoint_url="https://fixture-mcp.example.com/mcp",
        auth_secret_ref=None,
        enabled=enabled,
        status=McpServerStatus.READY,
        last_health_at=now,
        last_error=None,
        discovered_tools=tools if tools is not None else [_echo_snapshot(), _write_snapshot()],
        secret_hint=None,
        created_at=now,
        updated_at=now,
    )


def _echo_snapshot() -> dict[str, object]:
    """A server-annotated READ-ONLY tool snapshot (⇒ T0)."""
    return {
        "name": "echo",
        "description": "Echo text back.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "read_only": True,
    }


def _write_snapshot() -> dict[str, object]:
    """An UNANNOTATED (write-capable) tool snapshot (⇒ default T2, approval-gated)."""
    return {
        "name": "send_email",
        "description": "Send an email.",
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "required": ["to"],
        },
        "read_only": False,
    }


class _RecordingInvoker:
    """An :class:`McpInvoker` fake: records calls, returns a canned ok result."""

    def __init__(self, *, result: McpToolResult | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result or McpToolResult(ok=True, content="echo: hi")

    async def __call__(
        self, config: McpServerConfig, name: str, args: dict[str, Any]
    ) -> McpToolResult:
        self.calls.append((name, args))
        return self._result


# --- bridge: namespacing + tiering ------------------------------------------


def test_discovered_tools_are_namespaced_and_risk_tiered(world: _World) -> None:
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    invoker = _RecordingInvoker()
    tools = tools_for_servers([server], invoker)

    slug = slug_for_server(server)
    echo_name = namespaced_tool_name(slug, "echo")
    write_name = namespaced_tool_name(slug, "send_email")
    assert set(tools) == {echo_name, write_name}

    # A server-annotated read-only tool → T0, no approval, offered only on request.
    echo = tools[echo_name]
    assert echo.risk_tier is RiskTier.T0
    assert echo.read_only is True
    assert echo.requires_approval is False
    assert echo.default_offered is False

    # An unannotated (write-capable) tool → default T2, approval-gated (INV-7).
    write = tools[write_name]
    assert write.risk_tier is RiskTier.T2
    assert write.read_only is False
    assert write.requires_approval is True
    assert write.default_offered is False


def test_disabled_server_contributes_no_tools(world: _World) -> None:
    disabled = _server(tenant_id=world.tenant_id, owner_id=world.user_id, enabled=False)
    assert tools_for_servers([disabled], _RecordingInvoker()) == {}


# --- AC-1: an allow-listed read-only MCP tool is invokable through the runner ---


async def test_allowlisted_readonly_mcp_tool_runs_and_is_audited(world: _World) -> None:
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    invoker = _RecordingInvoker(result=McpToolResult(ok=True, content="echo: hi"))
    tools = tools_for_servers([server], invoker)
    echo_name = namespaced_tool_name(slug_for_server(server), "echo")

    r, audit_repo = _make_runner(
        world, allowed=frozenset({echo_name}), extra_tools=tools
    )
    result = await r.run(
        call=ToolCall(id="c1", name=echo_name, arguments={"text": "hi"}),
        context=_context(world),
    )
    assert result.ok is True
    assert result.content == "echo: hi"
    # The handler actually reached the adapter with the raw (un-namespaced) name.
    assert invoker.calls == [("echo", {"text": "hi"})]
    # Governed like a native tool: tool.invoked + tool.result + one trace row.
    actions = [e.action for e in await audit_repo.list_recent(limit=10)]
    assert AuditAction.TOOL_INVOKED.value in actions
    assert AuditAction.TOOL_RESULT.value in actions
    rows = await _all_invocations(world)
    assert len(rows) == 1 and rows[0].tool_name == echo_name and rows[0].ok is True


# --- allow-list: an MCP tool not named is not offered -----------------------


async def test_mcp_tool_not_in_allowlist_is_not_permitted(world: _World) -> None:
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    invoker = _RecordingInvoker()
    tools = tools_for_servers([server], invoker)
    echo_name = namespaced_tool_name(slug_for_server(server), "echo")

    # The tool is RESOLVED (registered + enabled) but NOT in this run's allow-list.
    r, _ = _make_runner(world, allowed=frozenset({"search_text"}), extra_tools=tools)
    result = await r.run(
        call=ToolCall(id="c1", name=echo_name, arguments={"text": "hi"}),
        context=_context(world),
    )
    assert result.ok is False
    assert result.error == ERROR_NOT_PERMITTED
    # The handler never ran — no outbound MCP call was made.
    assert invoker.calls == []
    rows = await _all_invocations(world)
    assert len(rows) == 1 and rows[0].error == ERROR_NOT_PERMITTED


# --- AC-3 / INV-7: a write-tier MCP tool blocks on the approval gate ---------


async def test_write_tier_mcp_tool_blocks_unapproved(world: _World) -> None:
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    invoker = _RecordingInvoker()
    tools = tools_for_servers([server], invoker)
    write_name = namespaced_tool_name(slug_for_server(server), "send_email")

    # Allow-listed but write-tier + default deny-all gate ⇒ NOT executed (INV-7).
    r, _ = _make_runner(world, allowed=frozenset({write_name}), extra_tools=tools)
    result = await r.run(
        call=ToolCall(id="c1", name=write_name, arguments={"to": "x@y.z"}),
        context=_context(world),
    )
    assert result.ok is False
    assert result.error == ERROR_APPROVAL_DENIED
    # The gate blocked BEFORE the handler — no outbound call happened.
    assert invoker.calls == []


async def test_write_tier_mcp_tool_runs_when_approved(world: _World) -> None:
    class _ApproveAll:
        async def request(self, request: ApprovalRequest) -> bool:
            return True

    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    invoker = _RecordingInvoker(result=McpToolResult(ok=True, content="sent"))
    tools = tools_for_servers([server], invoker)
    write_name = namespaced_tool_name(slug_for_server(server), "send_email")

    r, _ = _make_runner(
        world, allowed=frozenset({write_name}), extra_tools=tools, gate=_ApproveAll()
    )
    result = await r.run(
        call=ToolCall(id="c1", name=write_name, arguments={"to": "x@y.z"}),
        context=_context(world),
    )
    assert result.ok is True and result.content == "sent"
    assert invoker.calls == [("send_email", {"to": "x@y.z"})]


# --- AC-2 / INV-8: schema-invalid args → tool error, no outbound call --------


async def test_schema_invalid_args_are_rejected_before_the_call(world: _World) -> None:
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    invoker = _RecordingInvoker()
    tools = tools_for_servers([server], invoker)
    echo_name = namespaced_tool_name(slug_for_server(server), "echo")

    r, _ = _make_runner(world, allowed=frozenset({echo_name}), extra_tools=tools)
    # ``echo`` requires ``text: string``; supply the wrong type → a tool error.
    result = await r.run(
        call=ToolCall(id="c1", name=echo_name, arguments={"text": 123}),
        context=_context(world),
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    # A malformed call NEVER reaches the server.
    assert invoker.calls == []


async def test_missing_required_arg_is_rejected(world: _World) -> None:
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    invoker = _RecordingInvoker()
    tools = tools_for_servers([server], invoker)
    echo_name = namespaced_tool_name(slug_for_server(server), "echo")

    r, _ = _make_runner(world, allowed=frozenset({echo_name}), extra_tools=tools)
    result = await r.run(
        call=ToolCall(id="c1", name=echo_name, arguments={}),
        context=_context(world),
    )
    assert result.ok is False and result.error == ERROR_BAD_ARGS
    assert invoker.calls == []


# --- §7: a downed server → ok=False, never a crash --------------------------


async def test_downed_server_is_ok_false_not_a_crash(world: _World) -> None:
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    # The adapter contains a down server as a typed ``ok=False`` McpToolResult.
    down = McpToolResult.failure(
        error_code=MCP_ERROR_UNAVAILABLE, content="server unreachable"
    )
    invoker = _RecordingInvoker(result=down)
    tools = tools_for_servers([server], invoker)
    echo_name = namespaced_tool_name(slug_for_server(server), "echo")

    r, _ = _make_runner(world, allowed=frozenset({echo_name}), extra_tools=tools)
    result = await r.run(
        call=ToolCall(id="c1", name=echo_name, arguments={"text": "hi"}),
        context=_context(world),
    )
    # The run recovers: a result, not an exception. The adapter's code carries through.
    assert result.ok is False
    assert result.error == MCP_ERROR_UNAVAILABLE
    rows = await _all_invocations(world)
    assert len(rows) == 1 and rows[0].ok is False


# --- AC-4 / INV-1: the resolver never crosses tenant / owner ----------------


async def test_resolver_never_offers_a_disabled_or_foreign_server(
    world: _World,
) -> None:
    """The per-tenant resolver lists only THIS owner's ENABLED servers (INV-1/INV-2).

    Seed four servers in the DB: the caller's enabled one (contributes), the caller's
    disabled one (never), another owner's in the same tenant (never), and another
    tenant's (never). The resolver's tool map must contain ONLY the first server's
    tools.
    """
    # A second owner in the same tenant + a second tenant with its own owner.
    other_owner = await UserRepository(world.session, world.tenant_id).create(
        email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
    )
    tenant_b = await TenantRepository(world.session).create(name="Globex")
    carol = await UserRepository(world.session, tenant_b.id).create(
        email="carol@globex.test", password_hash="x", roles=[Role.MEMBER]
    )
    await world.session.commit()

    repo_a = McpServerRepository(world.session, world.tenant_id)
    repo_b = McpServerRepository(world.session, tenant_b.id)

    mine = await repo_a.create(
        owner_id=world.user_id,
        name="mine",
        transport="streamable_http",
        endpoint_url="https://a.example.com/mcp",
        auth_secret_ref=None,
        secret_hint=None,
    )
    await repo_a.update_health(
        mine.id,
        status=McpServerStatus.READY,
        last_health_at=None,
        last_error=None,
        discovered_tools=[_echo_snapshot()],
    )
    mine_disabled = await repo_a.create(
        owner_id=world.user_id,
        name="mine-disabled",
        transport="streamable_http",
        endpoint_url="https://a2.example.com/mcp",
        auth_secret_ref=None,
        secret_hint=None,
    )
    await repo_a.update_health(
        mine_disabled.id,
        status=McpServerStatus.READY,
        last_health_at=None,
        last_error=None,
        discovered_tools=[_echo_snapshot()],
    )
    await repo_a.update(mine_disabled.id, enabled=False)
    bobs = await repo_a.create(
        owner_id=other_owner.id,
        name="bobs",
        transport="streamable_http",
        endpoint_url="https://b.example.com/mcp",
        auth_secret_ref=None,
        secret_hint=None,
    )
    await repo_a.update_health(
        bobs.id,
        status=McpServerStatus.READY,
        last_health_at=None,
        last_error=None,
        discovered_tools=[_echo_snapshot()],
    )
    carols = await repo_b.create(
        owner_id=carol.id,
        name="carols",
        transport="streamable_http",
        endpoint_url="https://c.example.com/mcp",
        auth_secret_ref=None,
        secret_hint=None,
    )
    await repo_b.update_health(
        carols.id,
        status=McpServerStatus.READY,
        last_health_at=None,
        last_error=None,
        discovered_tools=[_echo_snapshot()],
    )
    await world.session.commit()

    # The repository read the resolver uses: only my enabled servers come back.
    enabled = await repo_a.list_enabled_for_owner(world.user_id)
    assert {s.id for s in enabled} == {mine.id}

    tools = tools_for_servers(enabled, _RecordingInvoker())
    assert set(tools) == {namespaced_tool_name(slug_for_server(mine), "echo")}
    # Bob's / Carol's / my-disabled slugs never appear.
    for foreign in (mine_disabled, bobs, carols):
        assert namespaced_tool_name(slug_for_server(foreign), "echo") not in tools


# --- the runner falls back to the static registry for a native name ---------


async def test_extra_tools_do_not_shadow_the_static_registry(world: _World) -> None:
    """A native tool still resolves when MCP ``extra_tools`` are present.

    The namespaces cannot collide (``mcp:*`` vs a bare name), so a run with MCP tools
    wired still resolves ``search_text`` from the static registry — MCP adds tools,
    it does not replace the native ones.
    """
    server = _server(tenant_id=world.tenant_id, owner_id=world.user_id)
    tools = tools_for_servers([server], _RecordingInvoker())
    r, _ = _make_runner(world, allowed=frozenset({"search_text"}), extra_tools=tools)
    # ``search_text`` is a real registered tool but not allow-listed as mcp; it is on
    # the allow-list here, so it resolves from the registry (its handler would need a
    # real retrieval — we only assert it was NOT treated as unknown/not-found).
    result = await r.run(
        call=ToolCall(id="c1", name="search_text", arguments={"query": "x"}),
        context=_context(world),
    )
    # Whatever the retrieval fake does, the tool RESOLVED (not tool_not_found /
    # not_permitted) — the static path still works alongside extra_tools.
    assert result.error not in {"tool_not_found", ERROR_NOT_PERMITTED}


# --- arg-validation unit coverage -------------------------------------------


def test_validate_args_allows_unknown_schema_shapes() -> None:
    # An empty schema imposes no constraint; a schema the check does not understand
    # is permitted through (the server validates authoritatively).
    assert mcp_bridge._validate_args({"anything": 1}, {}) is None
    assert mcp_bridge._validate_args({"x": 1}, {"type": "object"}) is None
    # bool is NOT an integer (a common JSON-Schema trap).
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert mcp_bridge._validate_args({"n": True}, schema) is not None
    assert mcp_bridge._validate_args({"n": 3}, schema) is None


# --- AC-1 end-to-end: real adapter + real SDK against the fixture server -----

_FIXTURE_HOST = "fixture-mcp.example.com"
_FIXTURE_URL = f"https://{_FIXTURE_HOST}/mcp"
_PUBLIC_IP = "93.184.216.34"


class _FakeLimiter:
    """Admits every request (no Redis); records the tenant it was asked about."""

    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    def try_acquire(self, tenant_id: uuid.UUID) -> bool:
        self.calls.append(tenant_id)
        return True


async def test_end_to_end_discovered_tool_invokes_through_the_real_adapter(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered server's tool is discovered, resolved, and invoked for real.

    Drives the WHOLE path offline: the real :class:`McpServersService` (registers +
    probes the in-process FastMCP fixture through the real SSRF guard + real MCP SDK
    handshake, discovering ``echo``/``boom``), then :meth:`resolve_run_tools` builds
    the namespaced ``ToolDefinition``s whose handlers invoke the real adapter, and the
    governed runner invokes ``echo`` end-to-end. The FastMCP fixture advertises no
    read-only annotation, so ``echo`` defaults to T2 (approval-gated) — an approving
    gate lets it run, proving the full invoke path reaches the fixture server.
    """
    from app.core.config import get_settings

    real = socket.getaddrinfo

    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        if host == _FIXTURE_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]
        return real(host, *args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)

    settings = get_settings()
    limiter = _FakeLimiter()

    async with fixture_mcp() as fx:
        factory = build_transport_client_factory(fx.inner_transport)

        def _service() -> Any:
            audit = AuditSink(AuditEventRepository(world.session, world.tenant_id))
            return build_mcp_servers_service(
                world.session,
                settings=settings,
                tenant_id=world.tenant_id,
                owner_id=world.user_id,
                roles=(Role.MEMBER,),
                audit=audit,
                request_id="req-e2e",
                source_ip="127.0.0.1",
                client_factory=factory,
                rate_limiter=limiter,
            )

        # Register + probe: the fixture's tools are discovered + persisted.
        server = await _service().register(
            name="fixture",
            transport="streamable_http",
            endpoint_url=_FIXTURE_URL,
            auth=None,
        )
        tested = await _service().test(server.id)
        assert tested is not None and tested.status is McpServerStatus.READY
        await world.session.commit()

        # Resolve the run's MCP tools from the registered+enabled server.
        tools = await _service().resolve_run_tools()

        echo_name = namespaced_tool_name(slug_for_server(server), "echo")
        assert echo_name in tools
        # Unannotated fixture tool ⇒ default T2, approval-gated (trust is earned).
        assert tools[echo_name].risk_tier is RiskTier.T2
        assert tools[echo_name].requires_approval is True

        class _ApproveAll:
            async def request(self, request: ApprovalRequest) -> bool:
                return True

        # Invoke through the governed runner while the fixture transport is live.
        r, _ = _make_runner(
            world, allowed=frozenset({echo_name}), extra_tools=tools, gate=_ApproveAll()
        )
        result = await r.run(
            call=ToolCall(id="c1", name=echo_name, arguments={"text": "hi"}),
            context=_context(world),
        )

    # The real fixture ``echo`` tool returns "echo: hi".
    assert result.ok is True
    assert "echo: hi" in result.content
