"""Audit-query use-case — the read side of the one audit sink (#85, #80).

The orchestration behind ``GET /audit`` (ADR-0004: ``services/`` compose the
query; the router calls exactly one service and shapes its result). It is the
read counterpart of the single product-audit sink (``services.audit``, #23):
the sink is the only *write* path, this is the *read* path. The two never
share code.

**Tenancy (spec 0004 §2.1, INV-1).** Every query is bound to the caller's
resolved ``tenant_id`` (from the token, never request input). A caller in
tenant A can never phrase a query that reaches tenant B's events — the tenant
predicate leads every statement, mirroring the schema indexes #82 added
(``(tenant_id, action, ts)`` etc., each leading with ``tenant_id``). The
role-gate (INV-5 → 403, ``admin``/``security`` only) is enforced one layer up
in the router via ``require_roles`` — distinct from this tenant scope.

**Provenance.** Each stored event's JSON ``metadata`` is projected into the
contract's ``provenance``: the full payload as ``raw``, plus a ``candidates``
list of allow/exclude dispositions. When an event recorded explicit candidate
dispositions (``metadata["candidates"]``) those map through verbatim; otherwise
allow-candidates are synthesised from the retrieved ``document_ids`` the
retrieval/answer events record (spec 0004 §2.4). Nothing is invented — the
projection only re-shapes what the sink already persisted.

**Why this service issues the query, not a repository method.** The append-only
:class:`~app.db.repositories.AuditEventRepository` exposes ``record`` (the write
the sink uses) and ``list_recent`` (an unfiltered tail); it has no
filtered/keyset read, and extending it is a wave-0 shared-seam edit outside this
slice's ownership (ADR-0008 §1). This read-only, tenant-scoped query is composed
here against the ``db`` ORM model directly — a localized, reviewed exception to
"``db/`` owns SQL" forced by the ownership manifest, kept honest by the
tenant-predicate-leads-every-statement discipline the repositories themselves
use. It reads only; it never writes the append-only table.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import ValidationError
from app.db import models

# Pagination bounds mirror the contract's Limit parameter (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# Null-actor rows (system / anonymous) carry no user id; the actor filter accepts
# either sentinel and both resolve to ``actor_id IS NULL`` (the schema keeps no
# discriminator — spec 0004 §2.4 stores ``actor_id`` or null). A null actor reads
# back under this label unless metadata recorded an explicit ``actor_kind``.
_NULL_ACTOR_SENTINELS = frozenset({"system", "anonymous"})
_DEFAULT_NULL_ACTOR_LABEL = "system"


# --- Contract-shaped views (the router serialises these; no HTTP type here) --


@dataclass(frozen=True, slots=True)
class AuditCandidateView:
    """One retrieval candidate considered for a decision (contract ``AuditCandidate``).

    ``disposition`` is ``"allow"`` or ``"exclude"``; ``reason`` explains why; the
    optional ``score`` is the retrieval/rerank score when recorded.
    """

    resource_id: str
    disposition: str
    reason: str
    score: float | None = None


@dataclass(frozen=True, slots=True)
class AuditProvenanceView:
    """Why a decision was made (contract ``AuditProvenance``).

    ``candidates`` is always present (possibly empty) — the contract marks it
    required. ``raw`` is the event's full recorded metadata payload.
    """

    candidates: list[AuditCandidateView] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditEventView:
    """One audit event projected for the wire (contract ``AuditEvent``).

    The storage-faithful row mapped to the contract shape: ``actor`` is the user
    id or the ``system``/``anonymous`` label, ``event_type`` is the taxonomy
    action string, ``decision`` the outcome, and ``provenance`` the candidate +
    raw projection above.
    """

    id: UUID
    ts: datetime
    actor: str
    tenant_id: UUID
    event_type: str
    resource_id: str | None
    decision: str
    provenance: AuditProvenanceView


@dataclass(frozen=True, slots=True)
class AuditEventPage:
    """One page of audit events plus the opaque cursor for the next page."""

    items: list[AuditEventView]
    next_cursor: str | None


# --- Cursor codec (opaque; carries the boundary row id) ---------------------

# A short, stable prefix so an arbitrary base64 string that happens to decode to
# a uuid-shaped value is still rejected unless it was minted here.
_CURSOR_PREFIX = "aud:"


def _encode_cursor(event_id: UUID) -> str:
    """Encode a boundary event id as an opaque URL-safe cursor.

    The wire treats the cursor as opaque (contract ``Cursor``); only the id
    travels — the boundary's ``ts`` is resolved in-database, so the cursor stays
    small and dialect-independent (mirrors the collections/documents keyset).
    """
    raw = f"{_CURSOR_PREFIX}{event_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    """Decode an opaque cursor back into the boundary event id.

    Raises:
        ValidationError: the cursor is not one this server issued (malformed
            base64, missing prefix, or non-uuid payload). Fail-closed → 422
            rather than silently returning the first page (INV-8).
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    """Clamp the requested page size into the contract's [1, 100] band."""
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


# --- Provenance projection (metadata → candidates + raw) --------------------


def _candidate_from_mapping(entry: object) -> AuditCandidateView | None:
    """Map one recorded candidate mapping to a view, or ``None`` if malformed.

    Defensive by construction: the audit metadata is free-form JSON, so a
    candidate entry that is not a well-formed mapping with the required keys is
    skipped rather than allowed to break the read (the read must never fail on a
    historically-recorded payload).
    """
    if not isinstance(entry, dict):
        return None
    resource_id = entry.get("resource_id")
    disposition = entry.get("disposition")
    if not isinstance(resource_id, str) or disposition not in ("allow", "exclude"):
        return None
    reason = entry.get("reason")
    score = entry.get("score")
    # ``bool`` is a subclass of ``int`` — exclude it so a stray ``True`` is not
    # silently coerced to ``1.0``.
    score_value = (
        float(score) if isinstance(score, int | float) and not isinstance(score, bool) else None
    )
    return AuditCandidateView(
        resource_id=resource_id,
        disposition=disposition,
        reason=reason if isinstance(reason, str) else "",
        score=score_value,
    )


def _provenance_from_metadata(metadata: dict[str, object]) -> AuditProvenanceView:
    """Project a stored event's metadata into the contract ``provenance`` shape.

    Order of preference (most specific first), never inventing data:

    1. Explicit ``metadata["candidates"]`` — a list the producer recorded with
       allow/exclude dispositions and reasons; mapped through verbatim.
    2. Otherwise, allow-candidates synthesised from ``metadata["document_ids"]``
       (the retrieved-doc ids retrieval/answer events record, spec 0004 §2.4) —
       each an ``allow`` with a stable reason, optionally excluding any
       ``metadata["excluded_ids"]`` recorded alongside.
    3. Otherwise, an empty candidate list (the field is required, never absent).

    ``raw`` is always the full metadata payload so a reviewer sees exactly what
    was recorded (model id, query hash, etc.).
    """
    candidates: list[AuditCandidateView] = []

    explicit = metadata.get("candidates")
    if isinstance(explicit, list):
        for entry in explicit:
            mapped = _candidate_from_mapping(entry)
            if mapped is not None:
                candidates.append(mapped)
    else:
        document_ids = metadata.get("document_ids")
        if isinstance(document_ids, list):
            for doc_id in document_ids:
                if isinstance(doc_id, str):
                    candidates.append(
                        AuditCandidateView(
                            resource_id=doc_id, disposition="allow", reason="retrieved"
                        )
                    )
        excluded_ids = metadata.get("excluded_ids")
        if isinstance(excluded_ids, list):
            for doc_id in excluded_ids:
                if isinstance(doc_id, str):
                    candidates.append(
                        AuditCandidateView(
                            resource_id=doc_id, disposition="exclude", reason="excluded"
                        )
                    )

    return AuditProvenanceView(candidates=candidates, raw=dict(metadata))


class AuditQueryService:
    """Filtered, paginated, tenant-scoped reads of the append-only audit log.

    Constructed per-request with the session and the caller's resolved
    ``tenant_id`` (from the token — never request input). The role-gate
    (``admin``/``security``, INV-5) is applied by the router before this service
    is reached; this service guarantees only tenant scope (INV-1) and the wire
    shape. It reads; it never writes the append-only table.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def query(
        self,
        *,
        actor: str | None = None,
        event_type: str | None = None,
        resource_id: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> AuditEventPage:
        """Return one keyset page of the tenant's audit events (newest → oldest).

        Filters (all optional, ANDed): ``actor`` (a user id, or ``system`` /
        ``anonymous`` → null actor), ``event_type`` (a taxonomy action),
        ``resource_id``, and a ``[from_ts, to_ts)`` time window (inclusive lower,
        exclusive upper — the contract's ``from``/``to``). Ordered by
        ``(ts, id)`` descending with ``id`` the stable tiebreaker, so the order
        is deterministic even when two events share a timestamp. Fetches
        ``limit + 1`` rows to decide whether a next page exists, then drops the
        extra and re-encodes the boundary id into ``next_cursor``.

        Every statement is tenant-scoped (INV-1): the tenant predicate leads,
        matching the #82 composite indexes. A malformed ``actor`` value (not a
        sentinel and not a uuid) selects nothing rather than erroring — it simply
        cannot match a stored ``actor_id``.
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None

        conditions: list[ColumnElement[bool]] = [models.AuditEvent.tenant_id == self._tenant_id]

        if actor is not None:
            conditions.append(self._actor_predicate(actor))
        if event_type is not None:
            conditions.append(models.AuditEvent.action == event_type)
        if resource_id is not None:
            conditions.append(models.AuditEvent.resource_id == resource_id)
        if from_ts is not None:
            conditions.append(models.AuditEvent.ts >= from_ts)
        if to_ts is not None:
            conditions.append(models.AuditEvent.ts < to_ts)
        if after_id is not None:
            conditions.append(self._keyset_predicate(after_id))

        stmt = (
            select(models.AuditEvent)
            .where(*conditions)
            .order_by(models.AuditEvent.ts.desc(), models.AuditEvent.id.desc())
            .limit(page_size + 1)
        )
        rows = (await self._session.execute(stmt)).scalars().all()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        next_cursor = _encode_cursor(page_rows[-1].id) if has_more and page_rows else None
        return AuditEventPage(
            items=[self._to_view(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    # --- internal helpers ---------------------------------------------------

    def _actor_predicate(self, actor: str) -> ColumnElement[bool]:
        """Translate the ``actor`` filter into a SQL predicate.

        ``system``/``anonymous`` → ``actor_id IS NULL`` (the schema keeps no
        discriminator, so both sentinels select the same null-actor rows). A
        uuid string → equality on ``actor_id``. Anything else is not a valid
        actor id and is made to match nothing (fail-closed: an unparseable
        filter returns no rows rather than ignoring the filter).
        """
        if actor in _NULL_ACTOR_SENTINELS:
            return models.AuditEvent.actor_id.is_(None)
        try:
            actor_uuid = UUID(actor)
        except ValueError:
            # Not a sentinel and not a uuid: select nothing.
            return models.AuditEvent.id.is_(None)
        return models.AuditEvent.actor_id == actor_uuid

    def _keyset_predicate(self, after_id: UUID) -> ColumnElement[bool]:
        """Keyset boundary for DESC ordering by ``(ts, id)``.

        The next page is rows whose ``(ts, id)`` sorts strictly after the cursor
        row's. The boundary's ``ts`` is resolved by a correlated scalar subquery
        scoped to this tenant — so a foreign cursor id resolves to ``NULL`` and
        the predicate excludes everything (fail-closed, no cross-tenant leak),
        and no Python ``datetime`` crosses the wire (exact on Postgres *and* the
        SQLite the offline tests use).
        """
        boundary_ts = (
            select(models.AuditEvent.ts)
            .where(
                models.AuditEvent.tenant_id == self._tenant_id,
                models.AuditEvent.id == after_id,
            )
            .scalar_subquery()
        )
        return or_(
            models.AuditEvent.ts < boundary_ts,
            and_(
                models.AuditEvent.ts == boundary_ts,
                models.AuditEvent.id < after_id,
            ),
        )

    def _to_view(self, row: models.AuditEvent) -> AuditEventView:
        """Map one ORM row to the contract-shaped view (the read boundary)."""
        metadata = dict(row.event_metadata)
        return AuditEventView(
            id=row.id,
            ts=row.ts,
            actor=self._actor_label(row, metadata),
            tenant_id=row.tenant_id,
            event_type=row.action,
            resource_id=row.resource_id,
            decision=row.outcome,
            provenance=_provenance_from_metadata(metadata),
        )

    @staticmethod
    def _actor_label(row: models.AuditEvent, metadata: dict[str, object]) -> str:
        """Render the acting principal for the wire (contract ``AuditEvent.actor``).

        A user id renders as its uuid string. A null actor (system/anonymous)
        renders under its recorded ``actor_kind`` discriminator when the emitter
        left one in metadata, else the default ``system`` label — the schema
        keeps no actor-kind column (spec 0004 §2.4 stores ``actor_id`` or null).
        """
        if row.actor_id is not None:
            return str(row.actor_id)
        kind = metadata.get("actor_kind")
        if isinstance(kind, str) and kind in _NULL_ACTOR_SENTINELS:
            return kind
        return _DEFAULT_NULL_ACTOR_LABEL


__all__ = [
    "AuditCandidateView",
    "AuditEventPage",
    "AuditEventView",
    "AuditProvenanceView",
    "AuditQueryService",
]
