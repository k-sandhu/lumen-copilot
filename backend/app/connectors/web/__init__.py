"""Web URL connector — the first connector (ADR-0009 §2).

Paste a public URL; we fetch it through the SSRF-guarded chokepoint
(:mod:`app.connectors.web.fetch`) and ingest the readable text — a single page,
or the items of an RSS/Atom feed, or the URLs of a sitemap.xml (bounded). Zero
source-side setup (no app IDs, no OAuth).

Exposes the module-level ``CONNECTOR`` the auto-discovered registry
(:mod:`app.connectors.registry`, ADR-0008 §3) picks up — adding a connector means
dropping a package like this, with no edit to a shared registry.
"""

from app.connectors.web.connector import CONNECTOR, WebConnector, detect_mode

__all__ = ["CONNECTOR", "WebConnector", "detect_mode"]
