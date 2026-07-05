"""Auto-discovered tool registry (CC-7 #207 §1) — the read side of the gateway.

Scans the ``app.services.tools.impls`` package for **modules** that expose a
module-level ``TOOLS`` sequence of :class:`~app.services.tools.types.ToolDefinition`,
and builds the ``{name: ToolDefinition}`` map. Adding a tool means dropping
``impls/<tool>.py`` with a ``TOOLS`` in it and (optionally) adding its name to a
session/assistant allow-list — **no edit to this file** (the same scan pattern as
``connectors/registry.py`` and ``api/v1/__init__.py``, ADR-0008 §3).

Discovery is lazy + cached: the first lookup imports the impl modules and builds
the map; later lookups are O(1). The map is deterministic (sorted by module then
tool name) so two tools can never silently shadow each other — a duplicate
``name`` is a build-time error and raises.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from functools import lru_cache

from app.domain.llm import ToolSpec
from app.services.tools.types import ToolDefinition

#: The module-level attribute an impl module exposes to register its tools.
TOOLS_ATTR = "TOOLS"


class UnknownToolError(KeyError):
    """No tool is registered for the requested ``name`` (deny by default)."""


@lru_cache(maxsize=1)
def _registry() -> dict[str, ToolDefinition]:
    """Build the ``{name: ToolDefinition}`` map by scanning the impls package.

    Imports each **module** of ``app.services.tools.impls`` and keeps every
    :class:`ToolDefinition` in its ``TOOLS`` sequence. Sorted by module name (then
    definition order) so registration is deterministic; a duplicate tool ``name``
    across the whole registry is a programming error and raises.
    """
    import app.services.tools.impls as package

    found: dict[str, ToolDefinition] = {}
    module_names = sorted(
        info.name for info in pkgutil.iter_modules(package.__path__) if not info.ispkg
    )
    for mod_name in module_names:
        module = importlib.import_module(f"{package.__name__}.{mod_name}")
        definitions = getattr(module, TOOLS_ATTR, None)
        if definitions is None:
            continue
        for definition in definitions:
            if not isinstance(definition, ToolDefinition):  # pragma: no cover - guardrail
                continue
            if definition.name in found:  # pragma: no cover — a duplicate is a build mistake
                raise RuntimeError(f"duplicate tool name {definition.name!r} (in {mod_name})")
            found[definition.name] = definition
    return found


def get_tool(name: str) -> ToolDefinition:
    """Return the tool registered under ``name``, or raise (deny by default).

    Raises:
        UnknownToolError: no tool is registered for ``name``. The runner turns
            this into a typed ``tool_not_found`` result (the model sees it; the
            run continues) — an unknown tool never silently no-ops.
    """
    try:
        return _registry()[name]
    except KeyError as exc:
        raise UnknownToolError(name) from exc


def has_tool(name: str) -> bool:
    """Whether a tool is registered under ``name``."""
    return name in _registry()


def registered_names() -> frozenset[str]:
    """The set of tool names currently discoverable in the registry."""
    return frozenset(_registry())


def all_tools() -> tuple[ToolDefinition, ...]:
    """Every registered tool, deterministically ordered by name."""
    return tuple(_registry()[name] for name in sorted(_registry()))


def default_allowlist() -> frozenset[str]:
    """The default per-run allow-list for ad-hoc chat (issue #207 §2).

    The read-only retrieval tools every ad-hoc chat session gets when no
    assistant/session-specific allow-list is configured (E1 ``tool_allowlist``).
    Kept as the registered read-only, T0, **default-offered** tools — so a newly
    added tool that is off-by-default and admin/assistant-gated (``web_search``,
    which reaches outside the tenant's corpus and needs an explicit web-mode
    enable — ADR-0014 §5 / issue #219, marks ``default_offered=False``) is NOT
    silently granted to ad-hoc chat; enabling it is a deliberate allow-list
    change, preserving deny-by-default.
    """
    return frozenset(
        name
        for name, defn in _registry().items()
        if defn.read_only and not defn.requires_approval and defn.default_offered
    )


def tool_specs(names: Iterable[str] | None = None) -> tuple[ToolSpec, ...]:
    """Render tools to the ``llm/`` :class:`ToolSpec`s advertised to the model.

    Bridges the governed :class:`ToolDefinition` to the gateway's tool vocabulary
    (name + description + JSON-Schema parameters). ``names`` restricts the output
    to a specific allow-list (so the model is only *offered* tools it may call —
    the runner still enforces the allow-list as the hard chokepoint); ``None``
    renders every registered tool. Deterministically ordered by name.
    """
    registry = _registry()
    allowed = set(names) if names is not None else None
    return tuple(
        ToolSpec(
            name=registry[name].name,
            description=registry[name].description,
            parameters=registry[name].json_schema,
        )
        for name in sorted(registry)
        if allowed is None or name in allowed
    )


__all__ = [
    "TOOLS_ATTR",
    "UnknownToolError",
    "all_tools",
    "default_allowlist",
    "get_tool",
    "has_tool",
    "registered_names",
    "tool_specs",
]
