"""The kit's roster — and the gate that keeps it complete.

:data:`SUBJECTS` is what every kit test is parametrized over. It holds the real
``gdrive`` subject plus the synthetic one, so a proof that only works for Drive
fails here rather than at the next connector's merge.

:func:`acl_declaring_registry_types` reads the **live registry** and reports
which registered connectors declare ``map_acl``. ``test_mapping`` asserts every
one of them has a subject — so landing a second managed connector without an
INV-2 fixture is a red test, which is the mechanism behind the issue's "accepts
the next ACL-declaring connector without modification".
"""

from __future__ import annotations

from app.connectors.base import get_map_acl
from app.connectors.registry import get_connector, registered_types

from . import gdrive, synthetic
from .subject import AclSubject

SUBJECTS: tuple[AclSubject, ...] = (gdrive.SUBJECT, synthetic.SUBJECT)

# Ids pytest shows for each parametrization.
SUBJECT_IDS: tuple[str, ...] = tuple(s.name for s in SUBJECTS)


def acl_declaring_registry_types() -> frozenset[str]:
    """Registered connector types that declare the ``map_acl`` capability.

    Read through the same ``get_map_acl`` probe the framework uses to derive the
    ``acl_enforced`` write mode, so this set is exactly "the connectors whose
    documents are governed by the exclusive mirror-only mode".
    """
    return frozenset(name for name in registered_types() if get_map_acl(get_connector(name)))


def subject_for(name: str) -> AclSubject | None:
    """The kit subject registered for a connector name, if any."""
    return next((s for s in SUBJECTS if s.name == name), None)


__all__ = ["SUBJECTS", "SUBJECT_IDS", "acl_declaring_registry_types", "subject_for"]
