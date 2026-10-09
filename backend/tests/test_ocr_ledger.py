"""Tenant-bound cache, spend reservations and audit, offline SQLite."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db import models
from app.db.ocr import OcrRepository
from app.db.session import tenant_session_scope
from app.domain.ocr import OcrError, OcrPolicy, OcrResult
from app.tasks.ocr import DurableOcrLedger
from tests import test_ingestion_task as fixtures

sqlite_engine = fixtures.sqlite_engine


async def setup():
    tenant, document = await fixtures._seed_document(mime_type="application/pdf", key="key")
    from app.db.repositories import DocumentRepository

    async with tenant_session_scope(tenant) as session:
        claimed = await DocumentRepository(session, tenant).begin_ingestion(document)
        admin = (
            (await session.execute(select(models.User).where(models.User.tenant_id == tenant)))
            .scalars()
            .first()
        )
        admin.roles = ["admin"]
        policy = OcrPolicy(tenant, True, 2, Decimal("0.02"), Decimal("0.01"), 1, admin.id)
        await OcrRepository(session, tenant).configure(policy)
    return tenant, document, claimed.ingestion_attempts, policy


async def test_paid_page_cache_and_unknown_calls_block_repayment(sqlite_engine):
    tenant, doc, attempt, policy = await setup()
    ledger = DurableOcrLedger(tenant, doc, attempt)
    await ledger.begin("a" * 64, "fixture", 1, policy)
    with pytest.raises(OcrError, match="ocr_pending"):
        await ledger.begin("a" * 64, "fixture", 1, policy)
    result = OcrResult("generated text", "fixture", None, Decimal("0.0021"), 12, 1)
    await ledger.finish("a" * 64, "fixture", result, None)
    assert await ledger.cached("a" * 64, "fixture") == result
    with pytest.raises(OcrError, match="ocr_pending"):
        await ledger.begin("a" * 64, "fixture", 1, policy)
    await ledger.begin("b" * 64, "fixture", 2, policy)
    await ledger.finish("b" * 64, "fixture", None, "ocr_timeout")
    with pytest.raises(OcrError, match="ocr_pending"):
        await ledger.begin("b" * 64, "fixture", 2, policy)
    async with tenant_session_scope(tenant) as session:
        rows = (await session.execute(select(models.OcrTenantPolicy))).scalars().all()
        assert rows[0].pages_used == 2
        assert rows[0].cost_used_microusd == 12100
        audits = (
            (
                await session.execute(
                    select(models.AuditEvent).where(models.AuditEvent.action.like("document.ocr_%"))
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 4
        assert all("generated text" not in str(row.event_metadata) for row in audits)


async def test_default_off_and_wrong_tenant_is_denied(sqlite_engine):
    tenant, doc, attempt, policy = await setup()
    foreign = DurableOcrLedger(uuid4(), doc, attempt)
    from app.domain.ingestion_stages import StageOwnershipLost

    with pytest.raises(StageOwnershipLost):
        await foreign.cached("a" * 64, "fixture")
    disabled = OcrPolicy(tenant)
    async with tenant_session_scope(tenant) as session:
        await OcrRepository(session, tenant).configure(disabled)
    with pytest.raises(OcrError, match="ocr_disabled"):
        await DurableOcrLedger(tenant, doc, attempt).begin("a" * 64, "fixture", 1, policy)


async def test_concurrency_and_page_budgets_are_durable(sqlite_engine):
    tenant, doc, attempt, policy = await setup()
    ledger = DurableOcrLedger(tenant, doc, attempt)
    await ledger.begin("a" * 64, "fixture", 1, policy)
    with pytest.raises(OcrError, match="ocr_concurrency"):
        await ledger.begin("b" * 64, "fixture", 2, policy)
    await ledger.finish(
        "a" * 64, "fixture", OcrResult("one", "fixture", None, Decimal("0.002")), None
    )
    await ledger.begin("b" * 64, "fixture", 2, policy)
    await ledger.finish(
        "b" * 64, "fixture", OcrResult("two", "fixture", None, Decimal("0.002")), None
    )
    with pytest.raises(OcrError, match="ocr_budget"):
        await ledger.begin("c" * 64, "fixture", 3, policy)


async def test_missing_intent_audit_rolls_back_reservation(sqlite_engine, monkeypatch):
    tenant, doc, attempt, policy = await setup()

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.tasks.ocr.AuditSink.emit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await DurableOcrLedger(tenant, doc, attempt).begin("a" * 64, "fixture", 1, policy)
    async with tenant_session_scope(tenant) as session:
        assert (await session.execute(select(models.OcrPageCache))).scalars().all() == []
        row = (await session.execute(select(models.OcrTenantPolicy))).scalar_one()
        assert row.pages_used == 0 and row.cost_used_microusd == 0 and row.active_calls == 0


async def test_non_admin_cannot_approve_external_ocr(sqlite_engine):
    tenant, doc, attempt, policy = await setup()
    async with tenant_session_scope(tenant) as session:
        admin = (
            await session.execute(select(models.User).where(models.User.id == policy.approved_by))
        ).scalar_one()
        admin.roles = ["member"]
        with pytest.raises(OcrError, match="ocr_approval_required"):
            await OcrRepository(session, tenant).configure(policy)


async def test_cost_budget_blocks_before_dispatch(sqlite_engine):
    from dataclasses import replace

    tenant, doc, attempt, policy = await setup()
    policy = replace(policy, budget_usd=Decimal("0.001"))
    async with tenant_session_scope(tenant) as session:
        await OcrRepository(session, tenant).configure(policy)
    with pytest.raises(OcrError, match="ocr_budget"):
        await DurableOcrLedger(tenant, doc, attempt).begin("a" * 64, "fixture", 1, policy)
