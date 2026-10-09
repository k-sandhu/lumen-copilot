"""Independent classification queue, durable work recovery and changed-only bulk jobs."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.classification.rules import load_rules
from app.classification.taxonomy import load_taxonomy
from app.core.config import Settings, get_settings
from app.db.classification import ClassificationRepository
from app.db.repositories import AuditEventRepository, DocumentRepository, UserRepository
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.classification import ClassificationWork
from app.domain.decisions import DecisionPolicy
from app.domain.entities import AuditOutcome, Role
from app.ingestion.native import (
    NativeUnavailableError,
    classification_features,
    classification_token_count,
    render_canonical,
)
from app.ingestion.tokenizer_artifact import load_tokenizer_artifact
from app.llm.decisions import DecisionsGateway
from app.services.audit import AuditSink
from app.services.classification import classify
from app.services.classification_controls import ClassificationControls
from app.services.classification_ledger import DurableDecisionBudgetStore
from app.services.decisions_accounting import CanonicalDecisionLedger
from app.services.provider_models import build_model_route_resolver
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task


async def schedule_in_transaction(
    session: Any,
    tenant_id: UUID,
    document_id: UUID,
    *,
    input_json: str,
    extraction_id: str,
    taxonomy_version: str,
) -> bool:
    repo = ClassificationRepository(session, tenant_id)
    controls = await repo.policy()
    version = str(controls.get("taxonomy_version", taxonomy_version))
    changed = await repo.schedule(
        document_id,
        extraction_id=extraction_id,
        input_json=input_json,
        taxonomy_version=version,
        controls=controls,
    )
    if changed:
        await AuditSink(AuditEventRepository(session, tenant_id)).emit(
            action=AuditAction.DOCUMENT_CLASSIFICATION_UPDATED,
            actor=AuditActor.system(),
            resource_type="document",
            resource_id=str(document_id),
            outcome=AuditOutcome.ALLOWED,
            request_id="document-classification-task",
            source_ip="system",
            metadata={
                "operation": "scheduled",
                "extraction_id": extraction_id,
                "taxonomy_version": version,
            },
        )
    return changed


async def _compute(
    work: ClassificationWork, controls: ClassificationControls, settings: Settings
) -> dict[str, Any]:
    if not controls.enabled or not settings.decisions_enabled:
        return {
            "status": "unclassified",
            "reason": "decision_disabled",
            "retryable": False,
            "path": None,
            "taxonomy_version": work.taxonomy_version,
        }
    assert controls.per_call_ceiling_usd is not None and controls.budget_usd is not None
    assert controls.excerpt_tokens is not None and controls.excerpt_chars is not None
    assert controls.input_tokens is not None
    artifact = load_tokenizer_artifact(
        settings.classification_tokenizer_path, sha256=settings.classification_tokenizer_sha256
    )
    if settings.classification_tokenizer_model != controls.model:
        raise ValueError("classification tokenizer model mismatch")
    raw = json.loads(work.input_json)
    if hashlib.sha256(work.input_json.encode()).hexdigest() != work.extraction_id:
        raise ValueError("classification source checksum mismatch")
    # Preserve a supplied canonical model; Python's flat extraction is labelled explicitly.
    canonical = raw.get("canonical") or {
        "blocks": [{"id": "python-extraction", "text": raw["text"]}],
        "generation": {
            "source_sha256": raw.get("source_sha256"),
            "extraction_id": work.extraction_id,
            "parser_id": "python-preserved",
        },
    }
    doc = render_canonical(json.dumps(canonical, ensure_ascii=False))
    evidence = classification_features(
        doc,
        tokenizer_json=artifact,
        rules_json=json.dumps(load_rules(work.taxonomy_version)),
        max_tokens=controls.excerpt_tokens,
        max_excerpt_chars=controls.excerpt_chars,
        format=str(raw.get("format", "text")),
    )
    async with tenant_session_scope(work.tenant_id) as session:
        document = await DocumentRepository(session, work.tenant_id).get(work.document_id)
        if document is None:
            raise ValueError("classification document missing")
        if controls.approved_by is None:
            raise ValueError("classification requires a trusted approving administrator")
        administrator = await UserRepository(session, work.tenant_id).get(controls.approved_by)
        if administrator is None or Role.ADMIN not in administrator.roles:
            raise ValueError("classification administrator approval is no longer valid")
        resolver = build_model_route_resolver(
            settings=settings,
            tenant_id=work.tenant_id,
            owner_id=administrator.id,
            roles=tuple(administrator.roles),
            request_id="document-classification-task",
            source_ip="system",
        )
        route = await resolver(session, controls.model)
        from app.db.repositories import LlmProviderRepository
        from app.services.provider_models import is_provider_model_id, resolve_provider_model

        if (
            is_provider_model_id(controls.model)
            and await resolve_provider_model(
                controls.model, LlmProviderRepository(session, work.tenant_id)
            )
            is None
        ):
            raise ValueError("classification provider is unavailable")
        if route.api_base and route.api_base.rstrip("/") != "https://openrouter.ai/api/v1":
            raise ValueError("decisions provider origin is not approved")
        ledger = CanonicalDecisionLedger(
            DurableDecisionBudgetStore(work.tenant_id, work=work),
            AuditSink(AuditEventRepository(session, work.tenant_id)),
            actor=AuditActor.system(),
            resource_id=str(work.document_id),
            request_id="document-classification-task",
            source_ip="system",
        )
        bounded = settings.model_copy(
            update={
                "decisions_max_input_bytes": controls.input_bytes,
                "decisions_timeout_seconds": controls.timeout_seconds,
                "decisions_retries": controls.retries,
                "decisions_concurrency": controls.concurrency,
                "decisions_retry_backoff_seconds": controls.backoff_seconds,
            }
        )
        policy = DecisionPolicy(
            work.tenant_id,
            True,
            route.model,
            controls.per_call_ceiling_usd,
            controls.budget_usd,
            fallback_model=controls.fallback_model,
            fallback_structured_outputs=controls.fallback_structured_outputs,
            api_key=route.api_key,
        )
        gateway = DecisionsGateway(
            bounded,
            ledger=ledger,
            token_counter=lambda s: classification_token_count(s, artifact),
            max_input_tokens=controls.input_tokens,
            fallback_token_counter=_fallback_counter(controls, settings),
        )
        result = await classify(
            evidence,
            load_taxonomy(work.taxonomy_version),
            gateway,
            tenant_id=work.tenant_id,
            policy=policy,
        )
        result["requested_model"] = controls.model
        return result


def _fallback_counter(controls: ClassificationControls, settings: Settings) -> Any:
    if controls.fallback_model is None:
        return None
    if settings.classification_fallback_tokenizer_model != controls.fallback_model:
        raise ValueError("classification fallback tokenizer model mismatch")
    artifact = load_tokenizer_artifact(
        settings.classification_fallback_tokenizer_path,
        sha256=settings.classification_fallback_tokenizer_sha256,
    )
    return lambda text: classification_token_count(text, artifact)


async def classify_document_async(
    tenant_id: UUID, document_id: UUID, *, settings: Settings, compute: Any = _compute
) -> bool:
    async with tenant_session_scope(tenant_id) as session:
        repo = ClassificationRepository(session, tenant_id)
        controls = ClassificationControls.model_validate(await repo.policy())
        # Includes every cascade level, retry and fallback; a crashed lease becomes recoverable.
        lease = int((controls.timeout_seconds or 30) * 5 * ((controls.retries or 0) + 2) + 60)
        work = await repo.claim(
            document_id, run_id=uuid4(), lease_seconds=lease, max_active=controls.concurrency or 1
        )
    if work is None:
        return False
    try:
        result = await compute(work, controls, settings)
    except NativeUnavailableError:
        result = {
            "status": "unclassified",
            "reason": "classification_native_unavailable",
            "retryable": False,
            "path": None,
            "taxonomy_version": work.taxonomy_version,
        }
    except ValueError:
        result = {
            "status": "unclassified",
            "reason": "classification_invalid_configuration",
            "retryable": False,
            "path": None,
            "taxonomy_version": work.taxonomy_version,
        }
    except Exception:
        result = {
            "status": "unclassified",
            "reason": "classification_computation_failed",
            "retryable": False,
            "path": None,
            "taxonomy_version": work.taxonomy_version,
            "total_cost_usd": None,
        }
    result.update(input_fingerprint=work.input_fingerprint, extraction_id=work.extraction_id)
    async with tenant_session_scope(tenant_id) as session:
        published = await ClassificationRepository(session, tenant_id).complete(
            work,
            result,
            max_retries=controls.stage_retries or 0,
            backoff_seconds=controls.backoff_seconds or 1,
        )
        if published:
            await AuditSink(AuditEventRepository(session, tenant_id)).emit(
                action=AuditAction.DOCUMENT_CLASSIFICATION_UPDATED,
                actor=AuditActor.system(),
                resource_type="document",
                resource_id=str(document_id),
                outcome=AuditOutcome.ERROR
                if result["status"] in {"unclassified", "incomplete"}
                else AuditOutcome.ALLOWED,
                request_id="document-classification-task",
                source_ip="system",
                metadata={
                    "operation": "completed",
                    "status": result["status"],
                    "reason": result.get("reason"),
                    "input_fingerprint": work.input_fingerprint,
                    "taxonomy_version": work.taxonomy_version,
                },
            )
    return published


async def reclassify_changed_async(
    tenant_id: UUID, *, settings: Settings, batch_size: int = 100
) -> int:
    count = 0
    after = None
    while True:
        async with tenant_session_scope(tenant_id) as session:
            repo = ClassificationRepository(session, tenant_id)
            rows = await repo.batch(after=after, limit=batch_size)
            for row in rows:
                count += int(
                    await schedule_in_transaction(
                        session,
                        tenant_id,
                        row.document_id,
                        input_json=row.input_json,
                        extraction_id=row.extraction_id,
                        taxonomy_version=settings.classification_taxonomy_version,
                    )
                )
        if not rows:
            break
        after = rows[-1].document_id
    return count


def enqueue_classification(tenant_id: UUID, document_id: UUID) -> bool:
    from kombu.exceptions import OperationalError

    try:
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
            classify_document.apply_async(
                args=(str(tenant_id), str(document_id)),
                queue="classification",
                connection=connection,
                retry=False,
            )
        return True
    except OperationalError:
        return False


@celery_app.task(name="lumen.classify_document", acks_late=True)  # type: ignore[misc]
def classify_document(tenant_id: str, document_id: str) -> bool:
    return run_task(
        classify_document_async(UUID(tenant_id), UUID(document_id), settings=get_settings())
    )


@celery_app.task(name="lumen.reclassify_changed", acks_late=True)  # type: ignore[misc]
def reclassify_changed(tenant_id: str) -> int:
    return run_task(reclassify_changed_async(UUID(tenant_id), settings=get_settings()))


@celery_app.task(name="lumen.sweep_classification")  # type: ignore[misc]
def sweep_classification() -> int:
    async def run() -> int:
        from app.db.classification import classification_tenants

        sent = 0
        after = None
        while True:
            tenants = await classification_tenants(after=after, limit=100)
            if not tenants:
                break
            for tenant_id in tenants:
                async with tenant_session_scope(tenant_id) as session:
                    rows = await ClassificationRepository(session, tenant_id).batch(
                        limit=100, due_only=True
                    )
                for row in rows:
                    sent += int(enqueue_classification(tenant_id, row.document_id))
            after = tenants[-1]
        return sent

    return run_task(run())
