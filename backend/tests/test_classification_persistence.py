"""Offline tenant predicates, leases, overrides, accounting and independent readiness."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

import app.db.session as db_session
from app.db import models
from app.db.classification import ClassificationRepository, DecisionBudgetRepository
from app.db.repositories import DocumentRepository
from app.domain.decisions import DecisionAttempt, DecisionUsage
from app.domain.llm import TokenUsage
from app.tasks.classification import reclassify_changed_async
from tests import test_ingestion_task as fixtures

sqlite_engine = fixtures.sqlite_engine
_offline_index_store = fixtures._offline_index_store


@pytest.fixture(autouse=True)
def offline_classification_index_enqueue(monkeypatch):
    # Classification repair is independent of the broker and ingestion readiness.
    monkeypatch.setattr("app.tasks.index_sync.enqueue_index_sync", lambda *_: None)


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


def bounded_controls():
    return {
        "enabled": True,
        "provider_approved": True,
        "model": "fixture",
        "allowed_models": ["fixture"],
        "per_call_ceiling_usd": ".01",
        "budget_usd": ".05",
        "input_tokens": 4096,
        "excerpt_tokens": 128,
        "excerpt_chars": 512,
        "input_bytes": 32768,
        "timeout_seconds": 1,
        "retries": 0,
        "stage_retries": 2,
        "backoff_seconds": 60,
        "concurrency": 1,
    }


async def test_model_failure_and_missing_audit_cannot_change_search_readiness(
    sqlite_engine, monkeypatch
):
    from app.tasks.classification import classify_document_async

    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as s:
        await ClassificationRepository(s, tenant).set_policy(bounded_controls())
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "key", b"Synthetic search-ready evidence.")
    assert (
        await fixtures.ingest_document_async(
            tenant,
            document,
            settings=fixtures._settings(),
            object_store=store,
            gateway=fixtures._FakeGateway(),
        )
    ).status.value == "ready"

    async def failed(work, controls, settings):
        return {
            "status": "unclassified",
            "reason": "decision_timeout",
            "retryable": True,
            "path": None,
            "total_cost_usd": None,
        }

    assert await classify_document_async(
        tenant, document, settings=fixtures._settings(), compute=failed
    )
    async with db_session.tenant_session_scope(tenant) as s:
        assert (await DocumentRepository(s, tenant).get(document)).status.value == "ready"
        work = await ClassificationRepository(s, tenant).get(document)
        assert work.result["reason"] == "decision_timeout" and work.retries == 1


async def test_budget_ledger_rechecks_override_before_another_dispatch(sqlite_engine):
    from app.services.classification_ledger import DurableDecisionBudgetStore

    tenant, document = await seed()
    async with db_session.tenant_session_scope(tenant) as s:
        repo = ClassificationRepository(s, tenant)
        await repo.set_policy(bounded_controls())
        work = await repo.claim(document, run_id=uuid4(), lease_seconds=60)
    ledger = DurableDecisionBudgetStore(tenant, work=work)
    assert await ledger.reserve(attempt(tenant), Decimal(".01"), Decimal(".05"))
    async with db_session.tenant_session_scope(tenant) as s:
        await ClassificationRepository(s, tenant).override(
            document,
            expected_revision=0,
            path="other/other/other",
            actor=uuid4(),
            reason="synthetic",
            taxonomy_version="1.0.0",
        )
    assert not await ledger.reserve(attempt(tenant), Decimal(".01"), Decimal(".05"))


async def test_corrupted_source_never_enters_compute(sqlite_engine):
    from app.tasks.classification import classify_document_async

    tenant, document = await seed()
    async with db_session.tenant_session_scope(tenant) as s:
        await ClassificationRepository(s, tenant).set_policy(bounded_controls())

    async def forbidden(*args):
        raise AssertionError("corrupted source dispatched")

    assert await classify_document_async(
        tenant, document, settings=fixtures._settings(), compute=forbidden
    )
    async with db_session.tenant_session_scope(tenant) as s:
        assert (await ClassificationRepository(s, tenant).get(document)).result[
            "reason"
        ] == "classification_invalid_configuration"
