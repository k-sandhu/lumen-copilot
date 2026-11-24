"""Live PostgreSQL + loopback HTTP qualification for answer disclosure (#833)."""

from __future__ import annotations

import asyncio
import socket
import uuid
from decimal import Decimal

import pytest
import uvicorn
from alembic import command
from httpx import AsyncClient
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db_session, get_durable_audit_transactions_dep
from app.auth import Principal, mint_access_token
from app.core.config import get_settings
from app.db import models
from app.db.audit_transactions import DurableAuditTransactions
from app.db.repositories import (
    ChatSessionRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import MessageRole, Role
from app.main import create_app
from app.services.answer_budgets import AnswerBudgets
from tests.test_answer_ceilings import _answer, _done, _HostileGateway
from tests.test_chat_runtime import _Ctx
from tests.test_db_migration import _PG_URL, _alembic_config, _live, _swap_db


@_live
async def test_live_limit_http_reload_audit_isolation_and_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real migrated DB and HTTP, controlled model; own database is always dropped."""
    database = f"lumen_833_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(_swap_db(_PG_URL, "postgres"), isolation_level="AUTOCOMMIT")
    url = _swap_db(_PG_URL, database)
    engine = create_async_engine(url)
    audit_engine = create_async_engine(url)
    server = None
    server_task = None
    listener = None
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database}"'))
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        await asyncio.to_thread(command.upgrade, _alembic_config(), "0054_billed_cost")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="833-live")
            user = await UserRepository(session, tenant.id).create(
                email="owner@833.test", password_hash="x", roles=[Role.MEMBER]
            )
            other_tenant = await TenantRepository(session).create(name="833-other")
            other_user = await UserRepository(session, other_tenant.id).create(
                email="other@833.test", password_hash="x", roles=[Role.MEMBER]
            )
            chat = await ChatSessionRepository(session, tenant.id).create(
                owner_id=user.id, model="test/model", title="833 live"
            )
            historical_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO messages (id, tenant_id, session_id, role, content) "
                    "VALUES (:id, :tenant, :chat, 'assistant', 'Historical answer.')"
                ),
                {"id": historical_id, "tenant": tenant.id, "chat": chat.id},
            )
            billing_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO llm_usage (id, tenant_id, model, prompt_tokens, "
                    "completion_tokens, total_tokens, cached_prompt_tokens, cache_write_tokens, "
                    "cost_usd, unpriced_calls) "
                    "VALUES (:id, :tenant, 'history', 0, 0, 0, 0, 0, 0.125, 2)"
                ),
                {"id": billing_id, "tenant": tenant.id},
            )
            await session.commit()
        await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0055_answer_limit"
            )
            assert (
                await connection.scalar(
                    text("SELECT limit_kind FROM messages WHERE id=:id"), {"id": historical_id}
                )
                is None
            )
        principal = Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
        ctx = _Ctx(
            sessionmaker=factory,
            principal=principal,
            session_id=chat.id,
            document_id=uuid.uuid4(),
            chunk_id=uuid.uuid4(),
            tenant_id=tenant.id,
        )
        gateway = _HostileGateway()
        envelopes, executed, _runtime = await _answer(
            ctx, gateway, AnswerBudgets(max_tool_calls=2, max_calls_per_round=2)
        )
        terminal = _done(envelopes)
        assert terminal["limit"]["kind"] == "tool_calls"
        assert len(executed) == 2 and len(gateway.synthesis) == 1
        async with factory() as session:
            denied = list(
                (
                    await session.scalars(
                        select(models.ToolInvocation).where(models.ToolInvocation.ok.is_(False))
                    )
                ).all()
            )
            assert len(denied) == 38 and all(
                row.error == "answer_budget_exhausted" for row in denied
            )
            audits = list(
                (
                    await session.scalars(
                        select(models.AuditEvent).where(models.AuditEvent.outcome == "denied")
                    )
                ).all()
            )
            assert len(audits) == 76
            assert {row.event_metadata["denied_reason"] for row in audits} == {"tool_calls"}
        # Real HTTP listener; DB and audit engines remain independent.
        app = create_app()

        async def request_session():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db_session] = request_session
        app.dependency_overrides[get_durable_audit_transactions_dep] = (
            lambda: DurableAuditTransactions(audit_engine, operation_timeout_seconds=5)
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0)
        settings = get_settings()
        headers = {"Authorization": "Bearer " + mint_access_token(principal, settings).token}
        other = Principal(user_id=other_user.id, tenant_id=other_tenant.id, roles=(Role.MEMBER,))
        other_headers = {"Authorization": "Bearer " + mint_access_token(other, settings).token}
        async with AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}") as client:
            path = f"/api/v1/chat/sessions/{chat.id}/messages"
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, response.text
            messages = {row["id"]: row for row in response.json()["items"]}
            assert messages[terminal["messageId"]]["limit"] == terminal["limit"]
            assert "limit" not in messages[str(historical_id)]
            assert (await client.get(path, headers=other_headers)).status_code == 404
            assert (await client.get(path)).status_code == 401
            async with factory() as session:
                row = await MessageRepository(session, tenant.id).add_with_id(
                    message_id=uuid.uuid4(),
                    session_id=chat.id,
                    role=MessageRole.ASSISTANT,
                    content="Unknown legacy value.",
                    limit_kind="unknown-legacy",
                )
                await session.commit()
            response = await client.get(path, headers=headers)
            messages = {item["id"]: item for item in response.json()["items"]}
            assert "limit" not in messages[str(row.id)]
        async with factory() as session:
            denied_access = list(
                (
                    await session.scalars(
                        select(models.AuditEvent).where(
                            models.AuditEvent.action == "permission.denied"
                        )
                    )
                ).all()
            )
            assert len(denied_access) == 1 and denied_access[0].tenant_id == other_tenant.id
        server.should_exit = True
        await server_task
        server_task = None
        await asyncio.to_thread(command.downgrade, _alembic_config(), "0054_billed_cost")
        async with engine.connect() as connection:
            columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("messages"))
            assert "limit_kind" not in {column["name"] for column in columns}
            assert (
                await connection.scalar(
                    text("SELECT content FROM messages WHERE id=:id"), {"id": historical_id}
                )
                == "Historical answer."
            )
            assert (
                await connection.execute(
                    text("SELECT cost_usd, unpriced_calls FROM llm_usage WHERE id=:id"),
                    {"id": billing_id},
                )
            ).one() == (Decimal("0.125"), 2)
        await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM messages WHERE limit_kind IS NOT NULL")
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM llm_usage WHERE id=:id"), {"id": billing_id}
                )
                == 1
            )
        await asyncio.to_thread(command.downgrade, _alembic_config(), "base")
        async with engine.connect() as connection:
            assert set(await connection.run_sync(lambda conn: inspect(conn).get_table_names())) <= {
                "alembic_version"
            }
    finally:
        if server_task is not None:
            server.should_exit = True
            await server_task
        if listener is not None:
            listener.close()
        await engine.dispose()
        await audit_engine.dispose()
        get_settings.cache_clear()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        await admin.dispose()
