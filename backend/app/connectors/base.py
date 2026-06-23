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

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.entities import Source


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


@dataclass(frozen=True, slots=True)
class FetchedDoc:
    """One document a connector fetched — a pure domain value (ADR-0004).

    The connector has already extracted **readable plain text** (HTML → text;
    ``text/*`` as-is); the sync task feeds ``text`` straight into the existing
    ingestion pipeline (chunk → embed → index, #21). ``title`` names the document
    row; ``url`` is the canonical source location (the page/feed-item/sitemap-url
    this passage came from), carried for provenance and citations.
    """

    title: str
    text: str
    url: str


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """A connector's reachability/validity probe result (ADR-0009 §4)."""

    healthy: bool
    detail: str | None = None


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

    async def sync(self, source: Source) -> Iterable[FetchedDoc]:
        """Fetch the source and return its documents as :class:`FetchedDoc`.

        Runs only inside the Celery sync task (never the request path). Raises
        :class:`ConnectorError` on a fetch fault. May re-validate (SSRF) as it
        fetches — for the web connector every redirect hop is re-checked.
        """
        ...

    async def health(self, source: Source) -> ConnectorHealth:
        """A cheap reachability/validity probe for the connector grid."""
        ...


# Sentinel each connector module exposes so the registry can discover it
# (ADR-0008 §3): ``connectors/<name>/__init__.py`` sets ``CONNECTOR = <impl>()``.
CONNECTOR_ATTR = "CONNECTOR"

__all__ = [
    "CONNECTOR_ATTR",
    "Connector",
    "ConnectorConfigError",
    "ConnectorError",
    "ConnectorHealth",
    "FetchedDoc",
]
