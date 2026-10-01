"""R4-002: all five authenticated sandbox target guards have one audit owner."""

from uuid import uuid4

import pytest

from app.core.errors import DependencyError
from app.db.repositories import CodeRunRepository
from tests import test_sandbox_sessions_api as sandbox_fixtures

app = sandbox_fixtures.app
client = sandbox_fixtures.client
fake_runner = sandbox_fixtures.fake_runner
seeded = sandbox_fixtures.seeded
sessionmaker_fixture = sandbox_fixtures.sessionmaker_fixture


ROUTES = [
    ("GET", "code", "", "code_run.read"),
    ("POST", "code", "/cancel", "code_run.cancel"),
    ("GET", "chat", "/sandbox", "sandbox_session.read"),
    ("POST", "chat", "/sandbox/reset", "sandbox_session.reset"),
    ("DELETE", "chat", "/sandbox", "sandbox_session.close"),
]


@pytest.mark.parametrize("method,kind,suffix,action", ROUTES)
@pytest.mark.parametrize("target", ["other_owner", "foreign", "unknown"])
async def test_sandbox_hidden_target_is_404_and_one_trusted_denial(
    method, kind, suffix, action, target, client, seeded, sessionmaker_fixture, durable_audit_ledger
):
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.alice_email, "password": "devpassword"}
    )
    token = login.json()["access_token"]
    chat_id = (
        seeded.bob_chat
        if target == "other_owner"
        else seeded.carol_chat
        if target == "foreign"
        else uuid4()
    )
    resource_id = chat_id
    if kind == "code" and target != "unknown":
        from sqlalchemy import select

        from app.db import models

        async with sessionmaker_fixture() as session:
            chat = (
                await session.execute(
                    select(models.ChatSession).where(models.ChatSession.id == chat_id)
                )
            ).scalar_one()
            run = await CodeRunRepository(session, chat.tenant_id).create(
                owner_id=chat.owner_id, code="private content"
            )
            resource_id = run.id
            await session.commit()
    path = (
        f"/api/v1/code-runs/{resource_id}{suffix}"
        if kind == "code"
        else f"/api/v1/chat/sessions/{resource_id}{suffix}"
    )
    response = await client.request(method, path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404, response.text
    assert len(durable_audit_ledger.events) == 1
    event = durable_audit_ledger.events[0]
    assert (event.tenant_id, event.actor_id) == (seeded.tenant_id, seeded.alice_id)
    assert event.action == "permission.denied"
    assert event.resource_id == str(resource_id)
    assert event.metadata == {"attempted_action": action, "reason": "not_visible"}
    assert event.request_id == response.headers["x-request-id"]
    assert event.source_origin == "client"


@pytest.mark.parametrize("method,kind,suffix,action", ROUTES)
async def test_sandbox_unauthenticated_and_sink_failure_do_not_disappear(
    method, kind, suffix, action, client, seeded, durable_audit_ledger
):
    path = (
        f"/api/v1/code-runs/{uuid4()}{suffix}"
        if kind == "code"
        else f"/api/v1/chat/sessions/{uuid4()}{suffix}"
    )
    response = await client.request(method, path)
    assert response.status_code == 401
    assert durable_audit_ledger.events == []
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.alice_email, "password": "devpassword"}
    )
    durable_audit_ledger.fail_with = DependencyError("audit unavailable")
    response = await client.request(
        method, path, headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    assert response.status_code == 503
    assert durable_audit_ledger.events == []
