"""Rolling session summarization — the async post-answer task (#416, ADR-0016 §3.2).

Enqueued (best-effort) after each successful chat answer; runs under a
tenant-bound Celery scope (the ADR-0015 pattern — ``tenant_session_scope``
binds the RLS GUC). Rolls turns older than the verbatim tail into the
session's persisted summary:

* The verbatim tail (``Settings.chat_summary_keep_messages``, newest M
  messages) is NEVER summarized — those turns reach the model word-for-word.
* The task no-ops until at least ``chat_summary_min_batch`` messages have
  accumulated beyond the tail, so short sessions never pay a summarize call.
* The summarizer prompt allows **conversational content only** — source
  documents travel as the IDs-only evidence digest, never as summary prose
  (ADR-0016 §3.2; the compression eval enforces the grounding half).
* A failure of ANY step is logged (type only) and swallowed — the hot answer
  path already completed, and the next answer degrades to the verbatim window
  (AC-4). The write is forward-only (the repository drops stale coverage), so
  two racing tasks converge on the newest boundary.
* The accepted write emits ``session.summarized`` (INV-6) and its token spend
  records to ``llm_usage`` (message-less, grouped by nothing — a maintenance
  spend attributed to the summarizer model).
"""

from __future__ import annotations

from uuid import UUID

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    CitationRepository,
    LlmUsageRepository,
    MessageRepository,
    SessionSummaryRepository,
)
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Message, MessageRole, Role
from app.domain.llm import ChatMessage
from app.domain.llm import Role as LlmRole
from app.llm import LLMGateway
from app.services.audit import AuditSink
from app.services.provider_models import (
    ModelRoute,
    build_model_route_resolver,
    is_provider_model_id,
)
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task

log = get_logger(__name__)

# The summarizer's system contract: conversational content ONLY. Document text
# must never enter the summary — evidence carries forward as ids and is
# re-fetched under CURRENT permissions (INV-2), so a summary that quoted a
# since-revoked document would be a leak-by-memory.
_SUMMARIZER_PROMPT = (
    "You maintain a rolling summary of a chat conversation. Produce an updated "
    "summary that merges the previous summary (if any) with the new turns.\n"
    "Rules:\n"
    "- Capture the user's goals, questions asked, answers' conclusions, and "
    "decisions/preferences stated in the conversation.\n"
    "- Refer to documents by NAME only. NEVER copy passages, quotes, figures, "
    "or tables from documents into the summary.\n"
    "- Be concise (under 300 words), neutral, and chronological.\n"
    "- Output ONLY the summary text."
)


def _config_summary_model(settings: Settings) -> str:
    """The config-side summarizer model: the pinned knob, else the registry default."""
    if settings.chat_summary_model:
        return settings.chat_summary_model
    default = next(
        (m for m in settings.chat_model_registry if m.is_default),
        settings.chat_model_registry[0],
    )
    return default.id


async def _resolve_summary_route(
    session: object,
    settings: Settings,
    *,
    tenant_id: UUID,
    owner_id: UUID,
    session_model: str,
) -> ModelRoute:
    """The summarizer's gateway route (#446 finding 6).

    A provider-only tenant must be able to summarize: the SESSION's model is
    resolved through the SAME permissioned provider-route seam chat uses
    (tenant-scoped, key decrypted inside services/). A config-pinned
    ``CHAT_SUMMARY_MODEL`` overrides; an unresolvable session model degrades
    to the config default with default credentials (the pre-#446 behavior).
    """
    if settings.chat_summary_model:
        return ModelRoute(model=settings.chat_summary_model)
    resolver = build_model_route_resolver(
        settings=settings,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=(Role.MEMBER,),
        request_id="summarize-task",
        source_ip="system",
    )
    try:
        route = await resolver(session, session_model)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — degrade to config default, never fail
        return ModelRoute(model=_config_summary_model(settings))
    if is_provider_model_id(session_model) and route.model == session_model:
        # Unresolved passthrough (provider vanished) — config default instead.
        return ModelRoute(model=_config_summary_model(settings))
    return route


async def _cited_documents_for(
    session: object, tenant_id: UUID, covered: list[Message]
) -> dict[UUID, str]:
    """{document_id: name} cited by the covered assistant turns (bounded)."""
    out: dict[UUID, str] = {}
    citations = CitationRepository(session, tenant_id)  # type: ignore[arg-type]
    for message in covered:
        if message.role is not MessageRole.ASSISTANT:
            continue
        for cit in await citations.list_for_message_hydrated(message.id):
            if cit.document_id not in out and len(out) < 20:
                out[cit.document_id] = cit.document_name
    return out


async def _cited_snippets_for(
    session: object, tenant_id: UUID, covered: list[Message]
) -> list[str]:
    """The covered turns' cited snippet texts — the redaction blocklist."""
    snippets: list[str] = []
    citations = CitationRepository(session, tenant_id)  # type: ignore[arg-type]
    for message in covered:
        if message.role is not MessageRole.ASSISTANT:
            continue
        for cit in await citations.list_for_message_hydrated(message.id):
            text = (cit.snippet or "").strip()
            if len(text) >= 20:
                snippets.append(text)
    return snippets


def _redact_cited_snippets(summary: str, snippets: list[str]) -> str:
    """Remove verbatim cited-source spans from the summary (#446 finding 1).

    Deterministic, not model-trusting: any cited snippet (≥20 chars, exact
    match) that leaked into the summarizer's output is cut. Conversational
    paraphrase survives; verbatim source text does not.
    """
    redacted = summary
    for snippet in snippets:
        if snippet in redacted:
            redacted = redacted.replace(snippet, "[cited source text removed]")
    return redacted


def _render_turns(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        speaker = "User" if m.role is MessageRole.USER else "Assistant"
        lines.append(f"{speaker}: {m.content}")
    return "\n\n".join(lines)


async def _summarize(tenant_id: UUID, session_id: UUID) -> str:
    settings = get_settings()
    async with tenant_session_scope(tenant_id) as session:
        summaries = SessionSummaryRepository(session, tenant_id)
        row = await summaries.get_for_session(session_id)
        all_messages = await MessageRepository(session, tenant_id).list_for_session(
            session_id
        )
        # Uncovered = everything after the current coverage boundary.
        start = 0
        if row is not None and row.covers_through_message_id is not None:
            cut = next(
                (
                    i
                    for i, m in enumerate(all_messages)
                    if m.id == row.covers_through_message_id
                ),
                None,
            )
            if cut is not None:
                start = cut + 1
        uncovered = all_messages[start:]
        keep = settings.chat_summary_keep_messages
        to_cover = uncovered[:-keep] if len(uncovered) > keep else []
        if len(to_cover) < settings.chat_summary_min_batch:
            return "skipped_below_batch"

        previous = row.summary if row is not None else None
        user_block = (
            (f"Previous summary:\n{previous}\n\n" if previous else "")
            + "New turns to fold in:\n"
            + _render_turns(to_cover)
        )
        chat_row = await ChatSessionRepository(session, tenant_id).get(session_id)
        if chat_row is None:
            return "skipped_no_session"
        route = await _resolve_summary_route(
            session,
            settings,
            tenant_id=tenant_id,
            owner_id=chat_row.owner_id,
            session_model=chat_row.model,
        )
        gateway = LLMGateway(settings)
        completion = await gateway.chat(
            [
                ChatMessage(role=LlmRole.SYSTEM, content=_SUMMARIZER_PROMPT),
                ChatMessage(role=LlmRole.USER, content=user_block),
            ],
            model=route.model,
            api_key=route.api_key,
            api_base=route.api_base,
            max_tokens=600,
        )
        summary_text = completion.content.strip()
        if not summary_text:
            return "skipped_empty_summary"
        boundary = to_cover[-1]
        # Names the summary may mention: the covered assistant turns' cited
        # documents (#446 finding 1) — recorded {id: name} so the READ path can
        # redact names whose documents the requester can no longer retrieve.
        mentioned = await _cited_documents_for(session, tenant_id, to_cover)
        # Structural leak guard (#446 finding 1): any cited snippet text that
        # made it into answers is REDACTED from the summary before persistence
        # — prompt rules are guidance; this is the deterministic backstop.
        summary_text = _redact_cited_snippets(
            summary_text, await _cited_snippets_for(session, tenant_id, to_cover)
        )
        if not summary_text.strip():
            return "skipped_empty_summary"
        accepted, updated = await summaries.upsert_summary(
            session_id,
            summary=summary_text,
            covers_through_message_id=boundary.id,
            covered_created_at=boundary.created_at,
            mentioned_documents=mentioned,
        )
        if not accepted:
            # A racing task already advanced further — nothing to audit.
            return "skipped_stale_coverage"
        # INV-6: the summarization is an audited write of conversational state
        # (counts + version only — never the summary text in metadata).
        await AuditSink(AuditEventRepository(session, tenant_id)).emit(
            action=AuditAction.SESSION_SUMMARIZED,
            actor=AuditActor.system(),
            resource_type="session",
            resource_id=str(session_id),
            outcome=AuditOutcome.ALLOWED,
            request_id="summarize-task",
            source_ip="system",
            metadata={
                "covered_messages": len(to_cover),
                "version": updated.version if updated is not None else None,
            },
        )
        # The summarizer's own spend is real — record it (message-less; the
        # session groups it; ADR-0016 §2.6 posture: no invisible spend).
        if completion.usage.total_tokens or completion.usage.prompt_tokens:
            await LlmUsageRepository(session, tenant_id).record(
                model=completion.model or route.model,
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                total_tokens=completion.usage.total_tokens,
                session_id=session_id,
            )
        return "summarized"


def enqueue_summarize(tenant_id: UUID, session_id: UUID) -> None:
    """Best-effort enqueue of the post-answer summarize (#446 finding 7).

    Owned by ``tasks/`` so ``api/`` never speaks to the Celery broker
    directly. Synchronous + blocking (call via ``asyncio.to_thread``); raises
    on broker failure — the caller decides that it is a nicety.
    """
    celery_app.send_task(
        "summarize_session", args=[str(tenant_id), str(session_id)]
    )


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="summarize_session",
    acks_late=True,
)
def summarize_session(tenant_id: str, session_id: str) -> dict[str, object]:
    """Celery entrypoint: roll the session's older turns into its summary.

    Best-effort by contract (AC-4): every failure is contained here — the
    answer already streamed, and assembly degrades to the verbatim window
    until a later attempt succeeds.
    """
    try:
        outcome = run_task(_summarize(UUID(tenant_id), UUID(session_id)))
    except Exception as exc:  # noqa: BLE001 — degrade, never propagate (AC-4)
        log.warning(
            "summarize.failed",
            session_id=session_id,
            error_type=type(exc).__name__,
        )
        return {"outcome": "failed", "error_type": type(exc).__name__}
    return {"outcome": outcome}
