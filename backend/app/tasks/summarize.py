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

from datetime import datetime
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
    """The summarizer's gateway route (#446 finding 6, #490).

    The summarizer runs on a DEDICATED model, not the session's answer route: a
    background compaction task must never inherit a frontier model just because
    the chat session picked one (#490). Resolution order:

    * A config-pinned ``CHAT_SUMMARY_MODEL`` always wins (explicit override,
      config credentials).
    * Otherwise a config (non-``provider:``) session uses the FAST-tier default
      (``_config_summary_model`` -> the registry ``is_default`` id) — NOT the
      session's model. This is the #490 fix: the leak was that an unpinned
      summarizer resolved the session's (possibly frontier) config id.
    * A provider-only session has no config route, so it still summarizes
      through the SAME permissioned provider-route seam chat uses (#446 finding
      6, tenant-scoped, key decrypted inside services/); an unresolvable
      provider degrades to the config default.
    """
    if settings.chat_summary_model:
        return ModelRoute(model=settings.chat_summary_model)
    if not is_provider_model_id(session_model):
        # A config session: the dedicated FAST default, never the answer route.
        return ModelRoute(model=_config_summary_model(settings))
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


# How many mentioned-document names one summary row may track: larger than the
# evidence cap because the map MERGES forward across passes (#446 r2 blocker
# 1b) — as long as a name may still live in the rolled-forward text, its id
# must stay redactable.
_MENTIONED_MAX = 40

# The most messages one summarize pass folds (#446 r2 finding 5): a backlog
# advances incrementally across enqueues instead of one unbudgeted prompt.
_MAX_BATCH_MESSAGES = 40


async def _covered_citations(
    session: object, tenant_id: UUID, covered: list[Message]
) -> tuple[dict[UUID, str], list[str]]:
    """({document_id: name}, [snippet texts]) for the covered turns — ONE query.

    A backlog batch must not issue per-message citation reads (#446 round-2
    new finding); both the mention map and the redaction blocklist derive from
    the same bounded batch.
    """
    message_ids = [m.id for m in covered if m.role is MessageRole.ASSISTANT]
    citations = CitationRepository(session, tenant_id)  # type: ignore[arg-type]
    rows = await citations.list_for_messages_hydrated_batch(message_ids)
    names: dict[UUID, str] = {}
    snippets: list[str] = []
    for cit in rows:
        if cit.document_id not in names and len(names) < _MENTIONED_MAX:
            names[cit.document_id] = cit.document_name
        text = (cit.snippet or "").strip()
        if len(text) >= 20:
            snippets.append(text)
    return names, snippets


# The smallest verbatim run the redactor hunts: windows of at least this many
# consecutive snippet words (and ≥ 15 chars) are treated as source text.
_REDACT_MIN_WINDOW_WORDS = 4
_REDACT_MIN_WINDOW_CHARS = 15


def _redact_cited_snippets(summary: str, snippets: list[str]) -> str:
    """Remove verbatim cited-source SPANS from the summary (#446 r2 blocker 1).

    Deterministic, not model-trusting, and windowed: every run of ≥
    ``_REDACT_MIN_WINDOW_WORDS`` consecutive words from any cited snippet that
    appears verbatim (case-insensitive) in the summarizer's output is cut —
    the whole-snippet-only match of round 1 let partial copies ("the
    acquisition price is $42.7M") survive. Longest windows are tried first so
    one replacement swallows its sub-windows. Honest residual (stated, not
    hidden): a PARAPHRASE is conversational content by the ADR's definition
    and survives; the prompt rule + this verbatim backstop are the contract.
    """
    redacted = summary
    marker = "[cited source text removed]"
    for snippet in snippets:
        words = snippet.split()
        if len(words) < _REDACT_MIN_WINDOW_WORDS:
            continue
        for size in range(len(words), _REDACT_MIN_WINDOW_WORDS - 1, -1):
            for start in range(0, len(words) - size + 1):
                window = " ".join(words[start : start + size])
                if len(window) < _REDACT_MIN_WINDOW_CHARS:
                    continue
                lowered = redacted.lower()
                needle = window.lower()
                while needle in lowered:
                    at = lowered.index(needle)
                    redacted = redacted[:at] + marker + redacted[at + len(window) :]
                    lowered = redacted.lower()
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
        messages_repo = MessageRepository(session, tenant_id)
        # Uncovered = everything after the durable coverage CURSOR, fetched in
        # SQL (#446 r2 finding 5) — valid after the boundary row is pruned,
        # never a full-history Python scan once a cursor exists.
        keep = settings.chat_summary_keep_messages
        fetch_limit = _MAX_BATCH_MESSAGES + keep
        if (
            row is not None
            and row.covers_through_message_id is not None
            and row.covers_through_created_at is not None
        ):
            fetched = await messages_repo.list_for_session_after(
                session_id,
                after_created_at=row.covers_through_created_at,
                after_message_id=row.covers_through_message_id,
                limit=fetch_limit,
            )
            # EXACT cut in Python (#446 round-3 liveness): the SQL window is
            # resend-tolerant across the format trap, but datetimes read back
            # compare exactly here — keep only rows STRICTLY greater than the
            # cursor in the (created_at, id-hex) total order, so the batch's
            # boundary is always one the CAS will accept.
            cursor = (row.covers_through_created_at, row.covers_through_message_id.hex)
            uncovered = [m for m in fetched if (m.created_at, m.id.hex) > cursor]
        else:
            # First-ever pass: the SAME ordered, limited query via a sentinel
            # cursor — a rowid-ordered initial batch would strand same-second
            # rows whose uuid sorts below the first boundary (they would fall
            # outside every later id-ordered window: silent LOSS, #446 r3).
            sentinel = datetime(1970, 1, 1)
            fetched = await messages_repo.list_for_session_after(
                session_id,
                after_created_at=sentinel,
                after_message_id=UUID(int=0),
                limit=fetch_limit,
            )
            uncovered = fetched
        # BOUNDED batch (#446 r2 finding 5): each pass folds at most
        # _MAX_BATCH_MESSAGES; a full fetch window means MORE rows exist
        # beyond it, so nothing fetched belongs to the verbatim keep-tail and
        # everything is coverable — the next enqueue continues the backlog.
        if len(fetched) >= fetch_limit:
            to_cover = uncovered[:_MAX_BATCH_MESSAGES]
        else:
            to_cover = uncovered[:-keep] if len(uncovered) > keep else []
            to_cover = to_cover[:_MAX_BATCH_MESSAGES]
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
        # Names the summary may mention (#446 finding 1): the covered turns'
        # cited documents — MERGED with the previous row's map (r2 blocker 1b:
        # the previous summary text rolls forward, so every name it may still
        # carry must stay redactable), newest-batch entries first.
        batch_names, batch_snippets = await _covered_citations(session, tenant_id, to_cover)
        mentioned = dict(batch_names)
        if row is not None:
            for doc_id, name in row.mentioned_documents:
                if doc_id not in mentioned and len(mentioned) < _MENTIONED_MAX:
                    mentioned[doc_id] = name
        # Structural leak guard (#446 finding 1): verbatim cited spans are
        # REDACTED from the summary before persistence — prompt rules are
        # guidance; this windowed match is the deterministic backstop.
        summary_text = _redact_cited_snippets(summary_text, batch_snippets)
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
    celery_app.send_task("summarize_session", args=[str(tenant_id), str(session_id)])


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
