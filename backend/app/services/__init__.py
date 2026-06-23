"""Services layer — use-cases / orchestration.

Single responsibility (ADR-0004 layering): the only layer that *composes*
adapters (``db``, ``llm``, ``retrieval``, ``storage``, ``tasks``, ``realtime``,
``auth``) to fulfil a use-case. Holds *what the app does*. Routers call exactly
one service and shape its result; services hold no HTTP concerns and return
domain types. Dependencies point inward (``api -> services -> domain``); a
service never imports a router.

**No re-export wall (ADR-0008 §3).** This package intentionally exports nothing.
Callers import directly from the concrete module —
``from app.services.<x>_service import ...`` (or ``app.services.audit`` etc.) —
so adding a service touches **only that service's file**, never this barrel. The
``services/__init__.py`` append-target that forced past rebases (#59/#46) is
retired; this file stays docstring-only.

The cross-cutting product-audit sink lives in :mod:`app.services.audit` (CC-8) —
the single ``emit(...)`` seam every later feature calls to satisfy the
"auditable" mission filter (spec 0004 §2.4).
"""
