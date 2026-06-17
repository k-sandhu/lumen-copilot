"""External source connectors — one adapter per external source.

Single responsibility (ADR-0004 boundary table): each connector lives under
``app/connectors/<name>/`` and is the **only** code that talks to that source's
API. A new external source means a new submodule here **and** a new row in the
boundary table, in the same change (AGENTS.md §7.6). Connector syncs run as
idempotent Celery tasks (``app.tasks``), never in the request path. No
connectors exist yet — this is the reserved namespace.
"""
