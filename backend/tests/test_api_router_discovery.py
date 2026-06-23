"""Smoke test for auto-discovery router registration (ADR-0008 §3, issue #79).

Proves the invariant that retires the ``api/v1/__init__.py`` append-target: the
aggregating ``router`` is assembled by *scanning* the ``app.api.v1`` package for
submodules exposing a module-level ``router``, in a deterministic sorted order —
not from a hand-edited include list.

Two things are asserted:

1. **Behavior is identical.** The five existing feature routers
   (auth/chat/collections/documents/models) still mount under ``/api/v1`` with
   exactly the same paths — discovery is sorted and complete.
2. **Zero-edit extensibility.** A throwaway module dropped into the
   ``app.api.v1`` package that exposes a ``router`` is auto-included *without*
   editing ``__init__.py`` — which is the whole point of the refactor.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import APIRouter

import app.api.v1 as v1

# The five feature routers that existed before the refactor — discovery must
# find all of them and nothing less (behavior-identical to the old manual list).
_EXPECTED_MODULES = {
    "app.api.v1.auth",
    "app.api.v1.chat",
    "app.api.v1.collections",
    "app.api.v1.documents",
    "app.api.v1.models",
}


def test_discovery_finds_every_feature_router() -> None:
    discovered = set(v1.discover_router_modules())
    assert _EXPECTED_MODULES <= discovered, (
        "auto-discovery dropped a known feature router: "
        f"missing {_EXPECTED_MODULES - discovered}"
    )


def test_discovery_order_is_deterministic_and_sorted() -> None:
    discovered = v1.discover_router_modules()
    assert discovered == sorted(discovered)


def test_aggregating_router_mounts_all_feature_paths() -> None:
    # Every route contributed by a discovered feature router is present on the
    # aggregating router, so app.main mounting `v1.router` keeps the same paths.
    aggregate_paths = {route.path for route in v1.router.routes}  # type: ignore[attr-defined]
    for module_name in _EXPECTED_MODULES:
        module = importlib.import_module(module_name)
        feature_paths = {route.path for route in module.router.routes}  # type: ignore[attr-defined]
        assert feature_paths <= aggregate_paths, (
            f"{module_name} routes missing from the aggregating router: "
            f"{feature_paths - aggregate_paths}"
        )


@pytest.fixture
def throwaway_router_module() -> Iterator[str]:
    """Drop a real ``api/v1/<x>.py`` exposing a ``router``, then remove it.

    Writing an actual file (not just a fake ``sys.modules`` entry) is what makes
    this a faithful smoke test: it exercises the same ``pkgutil.iter_modules`` +
    ``importlib`` path a future feature author would hit by adding a router file.
    """
    package_dir = Path(v1.__file__).parent
    module_name = "_throwaway_discovery_probe"
    module_path = package_dir / f"{module_name}.py"
    module_path.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n\n"
        '@router.get("/_throwaway_probe")\n'
        "async def _probe() -> dict[str, bool]:\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    full_name = f"app.api.v1.{module_name}"
    try:
        yield full_name
    finally:
        sys.modules.pop(full_name, None)
        module_path.unlink(missing_ok=True)


def test_new_router_module_is_auto_included_without_editing_init(
    throwaway_router_module: str,
) -> None:
    # The new module is discovered purely by being present in the package — the
    # invariant that adding a feature needs ZERO edits to a shared aggregator.
    discovered = v1.discover_router_modules()
    assert throwaway_router_module in discovered

    # And rebuilding the aggregating router (as app startup does) now mounts the
    # throwaway path — concrete proof the include happened with no __init__ edit.
    rebuilt = APIRouter()
    for module_name in discovered:
        rebuilt.include_router(importlib.import_module(module_name).router)
    assert "/_throwaway_probe" in {route.path for route in rebuilt.routes}  # type: ignore[attr-defined]


def test_services_barrel_has_no_reexport_wall() -> None:
    # The services re-export wall is gone (ADR-0008 §3): callers import directly
    # from app.services.<x>_service. The barrel defines no `__all__` and pulls no
    # service symbol up to the package level. (Submodules may appear in `vars()`
    # as a side effect of other modules importing them — that is NOT a re-export;
    # what we forbid is the barrel itself binding service classes/functions.)
    import app.services as services

    assert (
        getattr(services, "__all__", None) is None
    ), "app.services must not declare __all__ — the re-export wall is retired"
    forbidden = [
        "AuditSink",
        "ChatModelService",
        "CollectionsService",
        "DocumentService",
        "is_allowed_model",
    ]
    leaked = [name for name in forbidden if name in vars(services)]
    assert (
        leaked == []
    ), f"app.services re-exports {leaked}; import from app.services.<x>_service instead"
