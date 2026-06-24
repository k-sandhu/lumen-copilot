"""Auto-discovered connector registry (ADR-0008 §3 / ADR-0009 §1).

Scans the ``app.connectors`` package for **subpackages** that expose a
module-level ``CONNECTOR`` implementing the :class:`~app.connectors.base.Connector`
protocol, keyed by the connector's ``name`` (the ``SourceType`` value). Adding a
connector means dropping ``connectors/<name>/`` with a ``CONNECTOR`` — **no edit
to this file** (the same scan pattern as ``api/v1/__init__.py``).

Discovery is lazy + cached: the first lookup imports the subpackages and builds
the map; later lookups are O(1). The map is deterministic (sorted by name) so two
connectors can never silently shadow each other.
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import lru_cache

from app.connectors.base import CONNECTOR_ATTR, Connector


class UnknownConnectorError(KeyError):
    """No connector is registered for the requested ``type`` (deny by default)."""


@lru_cache(maxsize=1)
def _registry() -> dict[str, Connector]:
    """Build the ``{name: Connector}`` map by scanning the connectors package.

    Imports each **subpackage** of ``app.connectors`` (connectors live under
    ``connectors/<name>/``) and keeps the ones exposing a ``CONNECTOR`` that
    satisfies the :class:`Connector` protocol. Sorted by module name so the
    registration order is deterministic; a duplicate ``name`` is a programming
    error and raises.
    """
    import app.connectors as package

    found: dict[str, Connector] = {}
    module_names = sorted(
        info.name for info in pkgutil.iter_modules(package.__path__) if info.ispkg
    )
    for sub_name in module_names:
        module = importlib.import_module(f"{package.__name__}.{sub_name}")
        candidate = getattr(module, CONNECTOR_ATTR, None)
        if candidate is None or not isinstance(candidate, Connector):
            continue
        key = candidate.name
        if key in found:  # pragma: no cover — a duplicate is a build-time mistake
            raise RuntimeError(f"duplicate connector name {key!r} (in {sub_name})")
        found[key] = candidate
    return found


def get_connector(source_type: str) -> Connector:
    """Return the connector registered for ``source_type``, or raise.

    Raises:
        UnknownConnectorError: no connector is registered for the type. Deny by
            default — an unsupported ``type`` never silently no-ops (the API maps
            it to a 422 ``unsupported_source_type``).
    """
    try:
        return _registry()[source_type]
    except KeyError as exc:
        raise UnknownConnectorError(source_type) from exc


def registered_types() -> frozenset[str]:
    """The set of connector ``type`` values currently discoverable."""
    return frozenset(_registry())


__all__ = ["UnknownConnectorError", "get_connector", "registered_types"]
