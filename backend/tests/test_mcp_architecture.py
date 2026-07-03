"""AC-5 (architecture) — the MCP SDK is imported only inside ``app/mcp/``.

The ADR-0012 §2 boundary: ``app/mcp/`` is the ONLY importer of the official MCP
Python SDK (the top-level ``mcp`` distribution). If the SDK leaked into a router or
a service, the governance chokepoints (egress, secrets, failure isolation) would be
bypassable and the vendor lock-in would spread. Rather than assert "some module
doesn't leak", this proves the stronger structural invariant that makes a leak
*impossible to wire by accident*: a static AST scan of every non-test ``app/``
module for an ``import mcp`` / ``from mcp …``. It fails closed the moment a module
outside ``app.mcp`` reaches for the SDK.

Note the name collision: our subpackage is ``app.mcp`` while the SDK is the
top-level ``mcp`` — the scan is careful to flag only the *third-party* ``mcp``
(``import mcp`` / ``from mcp[.x] import``), never our own ``app.mcp``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_APP_ROOT = _BACKEND_ROOT / "app"
_MCP_PKG = _APP_ROOT / "mcp"


def _module_name(path: Path) -> str:
    """Dotted module name for an app/ file (e.g. app/api/v1/foo.py → app.api.v1.foo)."""
    rel = path.relative_to(_BACKEND_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _imports_mcp_sdk(path: Path) -> bool:
    """Whether ``path`` imports the third-party ``mcp`` SDK (not our ``app.mcp``).

    Matches ``import mcp`` / ``import mcp.x`` and ``from mcp import …`` /
    ``from mcp.x import …``. Our own package is always imported as ``app.mcp`` /
    ``from app.mcp …``, so it is never matched here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mcp" or alias.name.startswith("mcp."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) is never the top-level SDK.
            if node.level == 0:
                mod = node.module or ""
                if mod == "mcp" or mod.startswith("mcp."):
                    return True
    return False


def _app_py_files() -> list[Path]:
    return [p for p in _APP_ROOT.rglob("*.py") if p.is_file()]


def _is_inside_mcp_package(path: Path) -> bool:
    return _MCP_PKG in path.parents or path.parent == _MCP_PKG


def test_mcp_sdk_imported_only_inside_app_mcp() -> None:
    """AC-5 — no module outside ``app.mcp`` imports the MCP SDK.

    The whole point of the boundary: swap the SDK, add a transport, or fix a
    protocol quirk in exactly one place. Any importer outside ``app/mcp/`` is a
    boundary violation (and a governance-bypass risk).
    """
    offenders = [
        _module_name(p)
        for p in _app_py_files()
        if not _is_inside_mcp_package(p) and _imports_mcp_sdk(p)
    ]
    assert offenders == [], (
        "the MCP SDK (top-level `mcp`) must be imported ONLY inside app/mcp/ "
        f"(ADR-0012 §2 boundary); unexpected importers: {sorted(offenders)}"
    )


def test_app_mcp_is_the_boundary_and_does_import_the_sdk() -> None:
    """Sanity: ``app.mcp`` really is the owning module (it imports the SDK).

    Guards against the scan silently passing because *nothing* imports the SDK
    (e.g. a broken pattern): at least one module inside ``app/mcp/`` must import it,
    or the boundary assertion above proves nothing.
    """
    importers = [
        _module_name(p)
        for p in _app_py_files()
        if _is_inside_mcp_package(p) and _imports_mcp_sdk(p)
    ]
    assert importers, "expected app/mcp/ to import the MCP SDK (it is the adapter boundary)"


def test_no_api_module_imports_app_mcp() -> None:
    """No ``app.api`` router imports ``app.mcp`` — the adapter is used by services only.

    The MCP adapter is composed by services (#226/#227), never reached directly from
    a router (dependencies point inward: api → services → domain, adapters off
    services). Belt-and-braces so a route cannot open an MCP connection itself.
    """
    api_root = _APP_ROOT / "api"

    def _imports_app_mcp(path: Path) -> bool:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.mcp" or alias.name.startswith("app.mcp."):
                        return True
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "app.mcp" or mod.startswith("app.mcp."):
                    return True
        return False

    offenders = [
        _module_name(p) for p in api_root.rglob("*.py") if p.is_file() and _imports_app_mcp(p)
    ]
    assert (
        offenders == []
    ), f"an app.api module imports app.mcp — the adapter is service-composed only: {offenders}"
