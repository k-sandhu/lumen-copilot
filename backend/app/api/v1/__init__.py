"""Versioned API routes (``/api/v1``) — auto-discovered, no hand-edited include list.

The aggregating ``router`` is assembled by **scanning** this package for
submodules that expose a module-level ``router: APIRouter`` and including each in
a deterministic, sorted-by-module-name order (ADR-0008 §3, "auto-discovery
registration"). Adding a feature router means dropping ``api/v1/<x>.py`` with a
``router`` in it — **no edit to this file**. ``app.main`` mounts this package's
``router`` under the ``/api/v1`` prefix.

This retires the append-target that forced past rebases (#59/#46, #57/#19): the
file no longer carries a per-feature ``include_router(...)`` line.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter

__all__ = ["discover_router_modules", "router"]


def discover_router_modules() -> list[str]:
    """Return the sorted names of submodules in this package exposing a ``router``.

    Scans the ``app.api.v1`` package (non-recursively — feature routers live
    directly under it), imports each submodule, and keeps the ones with a
    module-level ``router`` that is an :class:`~fastapi.APIRouter`. The result is
    sorted by module name so route registration order is deterministic across
    machines and runs.
    """
    module_names: list[str] = []
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg:
            continue
        full_name = f"{__name__}.{module_info.name}"
        module = importlib.import_module(full_name)
        candidate = getattr(module, "router", None)
        if isinstance(candidate, APIRouter):
            module_names.append(full_name)
    return sorted(module_names)


def _build_router() -> APIRouter:
    """Assemble the aggregating v1 router from the discovered feature routers."""
    aggregate = APIRouter()
    for module_name in discover_router_modules():
        module = importlib.import_module(module_name)
        aggregate.include_router(module.router)
    return aggregate


# Aggregating router for all v1 feature routes. Mounted at /api/v1 in app.main.
router = _build_router()
