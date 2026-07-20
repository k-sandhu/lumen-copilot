"""Connector framework — the protocol + domain types (ADR-0009 §1).

The seam every external-source connector implements. A connector is the **only**
code that talks to its source (ADR-0004 boundary table); it exposes **domain
types only** — never a vendor SDK type — so a ``services``/``tasks`` caller sees
:class:`FetchedDoc`, never an ``httpx``/``feedparser``/``bs4`` object.

Three operations (ADR-0009 §1):

* :meth:`Connector.validate_config` — validate + normalise the user-supplied
  config (e.g. the web URL) *before* a row is written; raises
  :class:`ConnectorError` on a bad config (the API maps that to 422).
* :meth:`Connector.sync` — fetch the source and yield :class:`FetchedDoc`
  passages of readable text. Runs only inside the Celery sync task (never the
  request path).
* :meth:`Connector.health` — a cheap reachability/validity probe for the
  connector grid (ADR-0009 §4).

Connectors register by **auto-discovery** (:mod:`app.connectors.registry`,
ADR-0008 §3): adding a connector means dropping ``connectors/<name>/`` with a
module-level ``CONNECTOR`` — no edit to a shared registry.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

import httpx

from app.domain.entities import Source

if TYPE_CHECKING:
    from app.connectors.oauth import OAuthSpec


class ConnectorError(Exception):
    """A connector rejected its input or could not complete (domain error).

    A typed, vendor-free error: a bad config (``validate_config``) or a sync
    fault. The ``code`` is a stable, machine-readable discriminator the API maps
    to a Problem ``code`` (e.g. ``url_blocked`` for an SSRF rejection → 422).
    Subclasses narrow the meaning; raising this (never a vendor exception) keeps
    the boundary clean (ADR-0004).
    """

    def __init__(self, detail: str, *, code: str = "connector_error") -> None:
        self.detail = detail
        self.code = code
        super().__init__(detail)


class ConnectorConfigError(ConnectorError):
    """The user-supplied connector config is invalid (→ 422 at the API).

    Distinct from a transient sync fault: a config error is a permanent
    rejection of the input the user gave (a malformed or SSRF-blocked URL), so
    the API surfaces it as a validation failure, not a retryable error.
    """

    def __init__(self, detail: str, *, code: str = "invalid_config") -> None:
        super().__init__(detail, code=code)


class CursorExpiredError(ConnectorError):
    """The provider rejected the incremental cursor (Drive: HTTP 410).

    ADR-0019 §3: the framework clears ``sources.sync_cursor`` and falls back to
    a full resync — the connector only *signals*, it never touches the DB.
    """

    def __init__(self, detail: str = "the incremental cursor is expired") -> None:
        super().__init__(detail, code="cursor_expired")


@dataclass(frozen=True, slots=True)
class SourceAcl:
    """A document's mirrored source ACL — a pure domain value (ADR-0019 §2/§3).

    ``principals`` is the source allow-list normalized to Lumen principals
    (``user:<uuid>`` / ``tenant``) by the connector's pure ``map_acl`` — anything
    unmappable was already dropped (fail closed). ``scope_ids`` is the document's
    **container scope chain** (Drive: the drive id + ancestor folder ids), which
    the framework persists as ``documents.acl_scope_ids`` so a replayed container
    permission change can stale-stamp every known descendant (§3 cascade).
    """

    principals: frozenset[str]
    scope_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FetchedDoc:
    """One document a connector fetched — a pure domain value (ADR-0004).

    The connector has already extracted **readable plain text** (HTML → text;
    ``text/*`` as-is); the sync task feeds ``text`` straight into the existing
    ingestion pipeline (chunk → embed → index, #21). ``title`` names the document
    row; ``url`` is the canonical source location (the page/feed-item/sitemap-url
    this passage came from), carried for provenance and citations.

    ADR-0019 §3 widens this **additively** (defaults keep the ``web`` connector
    untouched):

    * ``external_id`` — the provider's stable id; enables identity-based
      reconcile (upsert by ``(source_id, external_id)``) instead of delete-all;
    * ``modified_at`` — the provider's last-modified stamp (provenance);
    * ``acl`` — the mirrored :class:`SourceAcl`; presence marks the document
      ``acl_enforced`` at the write seam (the mode is structural, never
      defaulted);
    * ``data``/``mime_type`` — a binary payload the ingestion pipeline parses
      (PDF/DOCX/…, ADR-0019 §5); ``data=None`` means ``text`` is the content
      and it is stored as ``text/plain`` exactly as before.
    """

    title: str
    text: str
    url: str
    external_id: str | None = None
    modified_at: datetime | None = None
    acl: SourceAcl | None = None
    data: bytes | None = None
    mime_type: str = "text/plain"


class PageIntegrity(str, enum.Enum):
    """Whether a replayed change page's cascade effects are provably complete.

    ``INCOMPLETE`` is the connector's fail-closed signal (ADR-0019 §3): it could
    not prove the affected-descendant set complete (missing scope data,
    enumeration failure, budget exhaustion) — the framework then stamps **every**
    ``acl_enforced`` document of the source stale (immediate deny).
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class SyncPage:
    """One replayed change page — the unit of atomic commit (ADR-0019 §3).

    The framework commits ``upserts`` + ``deleted_external_ids`` + the
    ``stale_scope_ids`` stamp + ``sources.sync_cursor = next_cursor`` in **one
    transaction**, so a crash between pages resumes from the last committed
    token — never skipping a page, never re-serving half-applied state. The
    cascade signals are first-class fields because connectors are forbidden
    from touching the DB. The terminal page's ``next_cursor`` is the provider's
    new baseline (Drive: ``newStartPageToken``).
    """

    upserts: tuple[FetchedDoc, ...]
    deleted_external_ids: frozenset[str]
    next_cursor: str
    stale_scope_ids: frozenset[str] = frozenset()
    integrity: PageIntegrity = PageIntegrity.COMPLETE


@dataclass(frozen=True, slots=True)
class FullSyncResult(Iterable[FetchedDoc]):
    """A capability-declaring connector's full-sync return value (ADR-0019 §3).

    Iterable of :class:`FetchedDoc` so it satisfies the base ``sync`` protocol
    unchanged (the ``web`` connector keeps returning a plain list). Carries the
    **start token captured before enumeration began** — the framework persists
    it as the source's ``sync_cursor`` after a successful full sync, so the
    first incremental replay covers the enumeration window; nothing that
    changed mid-enumeration is lost. ``skipped_count`` counts documents the
    connector deliberately did not emit (unsupported type, over the size cap,
    or a failed permissions fetch — the §2 fail-closed skip).
    """

    docs: tuple[FetchedDoc, ...]
    baseline_cursor: str | None = None
    skipped_count: int = 0

    def __iter__(self) -> Iterator[FetchedDoc]:
        return iter(self.docs)


@dataclass(frozen=True)
class AclMappingContext:
    """The frozen identity snapshot ``map_acl`` maps against (ADR-0019 §4).

    Framework-built (never by connector code): ``email_to_user_id`` contains
    **only attested users** of the connecting tenant (ADR-0019 §2 — unattested
    emails map to nothing), keyed by case-folded email. ``evaluated_at`` is the
    sync-time snapshot instant. Frozen + pure so ACL mapping is deterministic
    and unit-testable with zero mocks; connectors never read the DB.
    """

    email_to_user_id: Mapping[str, UUID]
    evaluated_at: datetime
    tenant_principal: str = "tenant"

    def principal_for_email(self, email: str) -> str | None:
        """The ``user:<uuid>`` principal for an **attested** email, or ``None``."""
        user_id = self.email_to_user_id.get(email.casefold())
        return f"user:{user_id}" if user_id is not None else None


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """A connector's reachability/validity probe result (ADR-0009 §4)."""

    healthy: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorRun:
    """The framework-supplied per-run execution context (ADR-0019 §4).

    Constructed by the **framework** (the sync task / health probe), never by
    connector code: for an OAuth connector, ``http`` is an already-authenticated,
    bounded HTTP client — the framework resolved the source's vault-held
    credential and attached it, so **a raw token is never handed to connector
    code** and a connector never reads the vault, the DB, or module state.
    For credential-less connectors (``web``) the context is anonymous and may be
    ignored — the web connector keeps using its own SSRF-guarded fetch
    chokepoint. The framework owns the client's lifecycle (close after the run).

    ``acl_context`` is the frozen attested-identity snapshot for a
    ``map_acl``-declaring connector (ADR-0019 §4) — built by the framework per
    run, ``None`` for connectors without the capability.
    """

    http: httpx.AsyncClient
    acl_context: AclMappingContext | None = None


def get_oauth_spec(connector: Connector) -> OAuthSpec | None:
    """The connector's OAuth capability, or ``None`` (ADR-0019 §4).

    A connector opts into the framework-driven OAuth flow by exposing
    ``oauth_spec() -> OAuthSpec``; presence marks its sources **managed**
    (admin-gated mutations, vault-held credentials). Absence — the ``web``
    connector — means the source type has no connect flow (the API maps that to
    409 ``oauth_not_supported``).
    """
    probe = getattr(connector, "oauth_spec", None)
    if probe is None or not callable(probe):
        return None
    spec: OAuthSpec = probe()
    return spec


def get_fetch_changes(
    connector: Connector,
) -> Callable[[Source, str, ConnectorRun], AsyncIterator[SyncPage]] | None:
    """The connector's incremental-sync capability, or ``None`` (ADR-0019 §4).

    Presence enables the §3 cursor-based replay: ``fetch_changes(source,
    cursor, run)`` yields :class:`SyncPage`\\ s the framework commits atomically
    per page. Absence means every sync is a full ``sync()``.
    """
    probe = getattr(connector, "fetch_changes", None)
    if probe is None or not callable(probe):
        return None
    fetch: Callable[[Source, str, ConnectorRun], AsyncIterator[SyncPage]] = probe
    return fetch


def get_map_acl(
    connector: Connector,
) -> Callable[[Mapping[str, object], AclMappingContext], frozenset[str]] | None:
    """The connector's ACL-mirroring capability, or ``None`` (ADR-0019 §4).

    Presence marks every document the connector produces ``acl_enforced=true``
    at the write seam (the exclusive enforcement mode, §2) — derived
    structurally from the connector, never defaulted. ``map_acl(raw, ctx)`` is
    pure over the frozen :class:`AclMappingContext` and fail-closed: anything
    it cannot map grants nobody.
    """
    probe = getattr(connector, "map_acl", None)
    if probe is None or not callable(probe):
        return None
    mapper: Callable[[Mapping[str, object], AclMappingContext], frozenset[str]] = probe
    return mapper


@runtime_checkable
class Connector(Protocol):
    """The interface every external-source connector implements (ADR-0009 §1).

    Implementations expose **domain types only** (ADR-0004). ``name`` is the
    connector key (the ``SourceType`` value, e.g. ``web``) the registry and the
    ``sources.type`` column use.
    """

    name: str

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        """Validate + normalise ``config``, returning the config to persist.

        Called in the request path *before* a ``sources`` row is written. Raises
        :class:`ConnectorConfigError` for an invalid config (the API maps it to
        422 with the error's ``code``, e.g. ``url_blocked`` for SSRF). The
        returned dict is what gets stored (e.g. the normalised URL; ``mode`` may
        be filled in later by ``sync`` once the content is fetched).
        """
        ...

    async def sync(self, source: Source, run: ConnectorRun) -> Iterable[FetchedDoc]:
        """Fetch the source and return its documents as :class:`FetchedDoc`.

        Runs only inside the Celery sync task (never the request path), against
        the framework-supplied :class:`ConnectorRun` (ADR-0019 §4 — an OAuth
        connector's ``run.http`` is already authenticated; credential-less
        connectors may ignore it). Raises :class:`ConnectorError` on a fetch
        fault. May re-validate (SSRF) as it fetches — for the web connector
        every redirect hop is re-checked.
        """
        ...

    async def health(self, source: Source, run: ConnectorRun) -> ConnectorHealth:
        """A cheap reachability/validity probe for the connector grid."""
        ...


# Sentinel each connector module exposes so the registry can discover it
# (ADR-0008 §3): ``connectors/<name>/__init__.py`` sets ``CONNECTOR = <impl>()``.
CONNECTOR_ATTR = "CONNECTOR"

__all__ = [
    "CONNECTOR_ATTR",
    "AclMappingContext",
    "Connector",
    "ConnectorConfigError",
    "ConnectorError",
    "ConnectorHealth",
    "ConnectorRun",
    "CursorExpiredError",
    "FetchedDoc",
    "FullSyncResult",
    "PageIntegrity",
    "SourceAcl",
    "SyncPage",
    "get_fetch_changes",
    "get_map_acl",
    "get_oauth_spec",
]
