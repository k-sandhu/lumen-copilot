"""Cascade probes for `gdrive` — induce each §3 cause in the REAL connector.

A probe drives ``CONNECTOR.fetch_changes`` (the production capability, through
the production guarded client) against a faked Drive REST server, under one
genuine failure condition, and returns the pages it emitted. The kit then
asserts the connector itself raised ``integrity=incomplete``.

That ordering matters: the framework's reaction to an ``INCOMPLETE`` page is a
single contract, so handing it a pre-formed incomplete page proves nothing about
whether *this* cause ever produces one. Budget exhaustion in particular had no
producer-side test anywhere before this module.

The Drive double is reused from ``tests/test_gdrive_connector.py`` rather than
re-implemented — two drifting fakes would be worse than the import coupling, and
the egress guard is stubbed exactly as that suite stubs it (public-range resolve
+ passthrough pin) so the REAL guard order runs without a socket.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from unittest import mock

import app.net.egress as net_egress
from app.connectors.base import SyncPage
from app.connectors.gdrive import CONNECTOR
from app.connectors.gdrive.connector import _CASCADE_REFETCH_BUDGET

# The Drive double + client/run helpers the connector suite already maintains.
from tests.test_gdrive_connector import (
    _GDOC,
    FakeDrive,
    _client,
    _drain,
    _folder_source,
    _folder_tree,
    _Run,
)

from .subject import CascadeProbe


@contextlib.contextmanager
def _guard_stubbed() -> Iterator[None]:
    """Public-range resolve + passthrough pin, so the REAL guard runs offline."""
    with (
        mock.patch.object(net_egress, "resolve_safe_ip", lambda host: "142.250.4.95"),
        mock.patch.object(net_egress, "pin_url_to_ip", lambda url, ip: url),
    ):
        yield


async def _replay(fake: FakeDrive) -> Sequence[SyncPage]:
    with _guard_stubbed():
        async with _client(fake) as http:
            return await _drain(
                CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http))  # type: ignore[arg-type]
            )


def _cascade_change(fake: FakeDrive, container_id: str) -> None:
    """A replayed permission change on a container inside the configured root."""
    fake.changes_pages = {
        "cur-1": {
            "changes": [
                {
                    "changeType": "file",
                    "fileId": container_id,
                    "file": fake.files[container_id],
                }
            ],
            "newStartPageToken": "baseline-probe",
        }
    }


async def _induce_enumeration_failure() -> Sequence[SyncPage]:
    """The descendant walk itself fails mid-cascade.

    ``sub`` is a folder inside the configured root, so its permission change
    cascades; making its child listing error out leaves the affected set
    unprovable.
    """
    fake = FakeDrive()
    _folder_tree(fake)
    fake.add_file("doc1", name="Doc", mime=_GDOC, parents=["sub"], export=b"x")
    fake.fail_children_of.add("sub")
    _cascade_change(fake, "sub")
    return await _replay(fake)


async def _induce_budget_exhaustion() -> Sequence[SyncPage]:
    """More affected descendants than one run may re-examine.

    The cascade root is re-walked with a per-run budget; a subtree larger than
    that budget cannot be re-examined in this run, so the affected set is
    unprovable even though every individual call succeeded. Uses the REAL
    ``_CASCADE_REFETCH_BUDGET`` — the mechanism is under test, not the number.
    """
    fake = FakeDrive()
    _folder_tree(fake)
    for index in range(_CASCADE_REFETCH_BUDGET + 1):
        fake.add_file(
            f"bulk{index}", name=f"Bulk {index}", mime=_GDOC, parents=["sub"], export=b"x"
        )
    _cascade_change(fake, "sub")
    return await _replay(fake)


async def _induce_healthy_cascade() -> Sequence[SyncPage]:
    """The control: an in-budget, fully enumerable cascade stays COMPLETE.

    Without this the probes could pass by a connector that reports INCOMPLETE
    unconditionally — which would be fail-closed but useless.
    """
    fake = FakeDrive()
    _folder_tree(fake)
    fake.add_file("doc1", name="Doc", mime=_GDOC, parents=["sub"], export=b"x")
    _cascade_change(fake, "sub")
    return await _replay(fake)


PROBES: dict[str, CascadeProbe] = {
    "enumeration_failure": CascadeProbe(
        id="enumeration_failure",
        why="the descendant listing of a changed container errors out",
        induce=_induce_enumeration_failure,
    ),
    "budget_exhausted": CascadeProbe(
        id="budget_exhausted",
        why="the changed container has more descendants than one run may re-examine",
        induce=_induce_budget_exhaustion,
    ),
    "healthy_cascade": CascadeProbe(
        id="healthy_cascade",
        why="control: an in-budget, fully enumerable cascade stays complete",
        induce=_induce_healthy_cascade,
    ),
}

__all__ = ["PROBES"]
