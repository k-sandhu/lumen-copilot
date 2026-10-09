"""Offline tenant predicates, leases, overrides, accounting and independent readiness."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

import app.db.session as db_session
from app.db import models
from app.db.classification import ClassificationRepository, DecisionBudgetRepository
from app.domain.decisions import DecisionAttempt, DecisionUsage
from app.domain.llm import TokenUsage
from app.tasks.classification import reclassify_changed_async
from tests import test_ingestion_task as fixtures

sqlite_engine = fixtures.sqlite_engine
_offline_index_store = fixtures._offline_index_store


async def seed():
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as s:
        repo = ClassificationRepository(s, tenant)
        await repo.set_policy({"enabled": True})
        assert await repo.schedule(
            document,
            extraction_id="a" * 64,
            input_json='{"text":"synthetic"}',
            taxonomy_version="1.0.0",
            controls={"enabled": True},
        )
    return tenant, document


async def test_schedule_is_idempotent_tenant_scoped_and_override_survives(sqlite_engine):
    tenant, document = await seed()
    async with db_session.tenant_session_scope(tenant) as s:
        repo = ClassificationRepository(s, tenant)
        assert not await repo.schedule(
            document,
            extraction_id="a" * 64,
            input_json='{"text":"synthetic"}',
            taxonomy_version="1.0.0",
            controls={"enabled": True},
        )
        work = await repo.claim(document, run_id=uuid4(), lease_seconds=60)
        assert work is not None
        assert await repo.override(
            document,
            expected_revision=work.revision,
            path="other/other/other",
            actor=uuid4(),
            reason="synthetic correction",
            taxonomy_version="1.0.0",
        )
        assert not await repo.complete(
            work, {"status": "classified", "path": "financial"}, max_retries=2, backoff_seconds=1
        )
        assert not await repo.schedule(
            document,
            extraction_id="b" * 64,
            input_json='{"text":"new"}',
            taxonomy_version="2.0.0",
            controls={"enabled": True},
        )
        assert (await repo.get(document)).override_path == "other/other/other"
    async with db_session.tenant_session_scope(uuid4()) as s:
        repo = ClassificationRepository(s, uuid4())
        assert await repo.get(document) is None
        assert await repo.claim(document, run_id=uuid4(), lease_seconds=60) is None


async def test_changed_input_fences_old_worker_and_retry_is_bounded(sqlite_engine):
    tenant, document = await seed()
    async with db_session.tenant_session_scope(tenant) as s:
        repo = ClassificationRepository(s, tenant)
        old = await repo.claim(document, run_id=uuid4(), lease_seconds=60)
        assert await repo.schedule(
            document,
            extraction_id="b" * 64,
            input_json='{"text":"changed"}',
            taxonomy_version="1.0.0",
            controls={"enabled": True},
        )
        assert not await repo.complete(
            old, {"status": "classified"}, max_retries=2, backoff_seconds=1
        )
        work = await repo.claim(document, run_id=uuid4(), lease_seconds=60)
        assert await repo.complete(
            work,
            {"status": "unclassified", "reason": "decision_timeout", "retryable": True},
            max_retries=2,
            backoff_seconds=60,
        )
        assert await repo.claim(document, run_id=uuid4(), lease_seconds=60) is None


def attempt(tenant):
    return DecisionAttempt(
        uuid4(), tenant, 1, "decisions", "fixture", "a" * 64, {"type": ("one", "two")}, "fixed", 0
    )


async def test_unknown_spend_keeps_ceiling_and_foreign_tenant_cannot_settle(sqlite_engine):
    tenant, _ = await seed()
    a = attempt(tenant)
    async with db_session.tenant_session_scope(tenant) as s:
        repo = DecisionBudgetRepository(s, tenant)
        assert await repo.reserve(a, Decimal(".02"), Decimal(".03"))
        assert await repo.reserve(a, Decimal(".02"), Decimal(".03"))
        await repo.settle(a, None)
        assert not await repo.reserve(attempt(tenant), Decimal(".02"), Decimal(".03"))
        await repo.settle(a, DecisionUsage(TokenUsage(10, 2, 12), Decimal(".001")))
        assert await repo.reserve(attempt(tenant), Decimal(".02"), Decimal(".03"))
    async with db_session.tenant_session_scope(uuid4()) as s:
        with pytest.raises(ValueError):
            await DecisionBudgetRepository(s, uuid4()).settle(a, None)


async def test_search_readiness_does_not_wait_for_classification(sqlite_engine, monkeypatch):
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "key", b"Synthetic document ready for search.")
    result = await fixtures.ingest_document_async(
        tenant,
        document,
        settings=fixtures._settings(),
        object_store=store,
        gateway=fixtures._FakeGateway(),
    )
    assert result.status.value == "ready"
    async with db_session.tenant_session_scope(tenant) as s:
        work = await ClassificationRepository(s, tenant).get(document)
        assert work.status == "unclassified" and work.result["reason"] == "decision_disabled"
        assert (
            await s.execute(select(models.Document.status).where(models.Document.id == document))
        ).scalar_one() == "ready"


async def test_changed_only_bulk_job_skips_same_inputs_and_overrides(sqlite_engine):
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as s:
        await ClassificationRepository(s, tenant).schedule(
            document,
            extraction_id="a" * 64,
            input_json='{"text":"synthetic"}',
            taxonomy_version="1.0.0",
            controls={"enabled": False},
        )
    assert await reclassify_changed_async(tenant, settings=fixtures._settings()) == 0
    assert (
        await reclassify_changed_async(
            tenant, settings=fixtures._settings(CLASSIFICATION_TAXONOMY_VERSION="1.0.1")
        )
        == 1
    )
