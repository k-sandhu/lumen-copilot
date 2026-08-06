"""Read-time re-check of stored evidence (#536 / #558, INV-2).

A citation is written only for a passage the asker could retrieve — but that is
a fact about **write** time. Revoke the grant afterwards and the row, including
the full ``chunks.text``, keeps coming back on every later read: the hydration
join is tenant-scoped only. Every surface that serves stored evidence must
therefore re-check it against **current** permissions.

Three surfaces do, and this module is what they share:

* the chat transcript (#536) — its citation list;
* the agent-run detail (#558) — its citation list **and** the durable step
  transcript, which re-serves the identical passage one field away;
* anything added later, which is the point of putting the rule here rather than
  in a third copy.

What is shared is the *decision* and the *record* — which documents the caller
may still read, and how withholding is audited. Each surface keeps its own
redaction, because only it knows the shape of what it serves.

**Redacted, not dropped.** Removing the evidence outright would erase a settled
claim's provenance, which is its own kind of dishonesty. The shell stays — with
``redacted=True`` and the disclosing fields emptied — so a UI can say "source no
longer available".

**The audit is counts-only.** A read that withheld content is permission-relevant
(INV-6), but the trail must never carry the text it just withheld, nor which
passage it was.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import CitationView
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.retrieval.permissions import AllowSet
from app.retrieval.queries import permitted_document_ids
from app.services.audit import AuditSink

#: Whatever the caller groups citations by — the message id on a transcript page,
#: a single run's message id on a run detail. Opaque here.
K = TypeVar("K")


async def resolve_permitted_documents(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    document_ids: Iterable[UUID],
) -> set[UUID]:
    """Which of these documents the caller may still retrieve, in one query.

    Goes through the same INV-2 chokepoint retrieval uses, so connector-ACL mode
    (ADR-0019 §2) and group principals (ADR-0022) are inherited rather than
    re-implemented. Callers pass the union of every document id on the response,
    so a surface serving the same passage twice pays for one lookup.
    """
    ids = sorted(set(document_ids))
    if not ids:
        return set()
    return await permitted_document_ids(session, allow_set=allow_set, document_ids=ids)


async def audit_withholding(
    audit: AuditSink | None,
    *,
    actor_id: UUID,
    surface: str,
    resource_type: str,
    resource_id: str,
    requested_documents: int,
    permitted_documents: int,
    redacted: int,
    request_id: str,
    source_ip: str,
) -> None:
    """Record that a read withheld content — counts only (INV-6).

    A no-op when nothing was withheld: an ordinary read is not an event. Also a
    no-op without a sink, because redaction must never be conditional on being
    able to record it — failing closed on the data matters more than the trail.

    Note:
        ``AuditSink.emit`` flushes but does not commit — the caller owns the
        transaction. A route calling this **must** commit, or the record it just
        wrote unwinds when the request-scoped session closes (the defect #554's
        review caught on the transcript path).
    """
    if redacted <= 0 or audit is None:
        return
    await audit.emit(
        action=AuditAction.EVIDENCE_REHYDRATED,
        actor=AuditActor.user(actor_id),
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=AuditOutcome.ALLOWED,
        request_id=request_id,
        source_ip=source_ip,
        metadata={
            "surface": surface,
            "requested_documents": requested_documents,
            "permitted_documents": permitted_documents,
            "redacted_citations": redacted,
        },
    )


async def enforce_citation_permissions(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    citations_by_key: Mapping[K, Sequence[CitationView]],
    surface: str,
    resource_type: str,
    resource_id: str,
    actor_id: UUID,
    audit: AuditSink | None,
    request_id: str,
    source_ip: str,
) -> dict[K, list[CitationView]]:
    """Redact every citation the requester may no longer retrieve, and audit it.

    The whole job for a surface whose only evidence is a citation list (the chat
    transcript). A surface that also serves the passage elsewhere — the run
    detail's step transcript — composes ``resolve_permitted_documents`` and
    ``audit_withholding`` itself, so its single permission query spans both.

    Returns the same mapping with unreadable citations replaced by their redacted
    shells; keys are untouched and nothing is dropped.
    """
    if not citations_by_key:
        return {key: list(cites) for key, cites in citations_by_key.items()}
    document_ids = {c.document_id for cites in citations_by_key.values() for c in cites}
    if not document_ids:
        return {key: list(cites) for key, cites in citations_by_key.items()}

    permitted = await resolve_permitted_documents(
        session, allow_set=allow_set, document_ids=document_ids
    )
    redacted_count = 0
    filtered: dict[K, list[CitationView]] = {}
    for key, cites in citations_by_key.items():
        out: list[CitationView] = []
        for citation in cites:
            if citation.document_id in permitted:
                out.append(citation)
            else:
                out.append(citation.redact())
                redacted_count += 1
        filtered[key] = out

    await audit_withholding(
        audit,
        actor_id=actor_id,
        surface=surface,
        resource_type=resource_type,
        resource_id=resource_id,
        requested_documents=len(document_ids),
        permitted_documents=len(permitted),
        redacted=redacted_count,
        request_id=request_id,
        source_ip=source_ip,
    )
    return filtered


__all__ = [
    "audit_withholding",
    "enforce_citation_permissions",
    "resolve_permitted_documents",
]
